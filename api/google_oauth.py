"""
Google OAuth utilities: authorization URL generation, code exchange, and building
per-user Google Drive/YouTube clients from stored tokens.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from core.constants import GOOGLE_INTEGRATION_SCOPES

from .config import settings
from .database import (
    Database,
    get_google_token,
    update_google_token_expiry,
    upsert_google_token,
)

logger = logging.getLogger(__name__)

AVAILABLE_GOOGLE_INTEGRATIONS = set(GOOGLE_INTEGRATION_SCOPES.keys())


def _normalize_integration(value: Optional[str]) -> str:
    if not value:
        raise ValueError("Google integration is required")
    key = value.lower()
    if key not in AVAILABLE_GOOGLE_INTEGRATIONS:
        raise ValueError(f"Unsupported Google integration '{value}'")
    return key


def _scopes_for_integration(integration: str) -> List[str]:
    key = _normalize_integration(integration)
    return GOOGLE_INTEGRATION_SCOPES[key]


def normalize_google_integration(value: Optional[str]) -> str:
    return _normalize_integration(value)


def get_google_oauth_url(state: str, redirect_uri: str, *, integration: str) -> str:
    """Build the Google OAuth consent URL using env-based client config.
    
    Args:
        state: CSRF state token
        redirect_uri: The callback URL to redirect to after OAuth (built from request URL)
    """
    if not settings.google_client_id or not settings.google_client_secret:
        raise ValueError("Google OAuth not configured")
    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=_scopes_for_integration(integration),
    )
    flow.redirect_uri = redirect_uri
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",  # Don't include previously granted scopes to avoid scope conflicts
        state=state,
        prompt="consent",  # Always show consent screen to ensure correct scopes
    )
    return auth_url


async def exchange_google_code(
    db: Database,
    user_id: str,
    code: str,
    redirect_uri: str,
    *,
    integration: str,
) -> None:
    """Exchange OAuth code for tokens and store them for the user.
    
    Args:
        db: Database instance
        user_id: User ID to store tokens for
        code: OAuth authorization code
        redirect_uri: The callback URL used in the OAuth flow (must match)
    """
    if not settings.google_client_id or not settings.google_client_secret:
        raise ValueError("Google OAuth not configured")
    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=_scopes_for_integration(integration),
    )
    flow.redirect_uri = redirect_uri
    
    # Manually exchange the code to avoid oauthlib's strict scope validation
    # This ensures we get the token even when Google grants additional scopes
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
        )
        if token_response.status_code != 200:
            error_detail = token_response.text
            logger.error(f"Token exchange failed: {error_detail}")
            raise ValueError(f"OAuth token exchange failed: {error_detail}")
        token_json = token_response.json()
    
    # Create credentials from manually obtained token
    scopes_list = token_json.get("scope", "").split() if isinstance(token_json.get("scope"), str) else (token_json.get("scope") or [])

    # Compute expiry from expires_in when present
    expires_in_raw = token_json.get("expires_in")
    expiry_dt = None
    try:
        if expires_in_raw is not None:
            expires_in_int = int(expires_in_raw)
            if expires_in_int > 0:
                expiry_dt = datetime.now(timezone.utc) + timedelta(seconds=expires_in_int)
    except Exception:
        expiry_dt = None

    creds = Credentials(
        token=token_json.get("access_token"),
        refresh_token=token_json.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=scopes_list,
        expiry=expiry_dt,
    )
    expiry_iso = None
    if creds.expiry:
        expiry_iso = creds.expiry.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    # Use the scopes Google actually granted (may include additional scopes)
    scopes_str = " ".join(creds.scopes or [])
    await upsert_google_token(
        db,
        user_id=user_id,
        integration=integration,
        access_token=creds.token,
        refresh_token=getattr(creds, "refresh_token", None),
        expiry=expiry_iso,
        token_type="Bearer",
        scopes=scopes_str,
    )


def parse_google_scope_list(raw_scopes: Optional[object]) -> List[str]:
    """Normalize scopes stored as json/text/list into a list of strings."""
    if not raw_scopes:
        return []
    if isinstance(raw_scopes, list):
        return [str(scope).strip() for scope in raw_scopes if str(scope).strip()]
    scope_text = str(raw_scopes).strip()
    if not scope_text:
        return []
    try:
        maybe = json.loads(scope_text)
        if isinstance(maybe, list):
            return [str(scope).strip() for scope in maybe if str(scope).strip()]
    except json.JSONDecodeError:
        pass
    normalized = [token.strip() for token in scope_text.replace(",", " ").split() if token.strip()]
    return normalized


async def _build_google_service_for_user(
    db: Database,
    user_id: str,
    *,
    integration: str,
    missing_scope_message: str,
    service_name: str,
    service_version: str,
):
    """Shared helper that loads/refreshes user creds and returns a Google API client."""
    integration_key = _normalize_integration(integration)
    token_row = await get_google_token(db, user_id, integration_key)
    if not token_row:
        raise ValueError("Google account not linked for this integration")

    scopes = parse_google_scope_list(token_row.get("scopes"))
    required_scopes = _scopes_for_integration(integration_key)
    if not all(scope in scopes for scope in required_scopes):
        raise ValueError(missing_scope_message)

    # Use the scopes that were actually granted (may include additional scopes like youtube.readonly)
    # This handles cases where Google grants both youtube and youtube.readonly
    creds = Credentials(
        token=token_row.get("access_token"),
        refresh_token=token_row.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=scopes,  # Use actual granted scopes, not just requested ones
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                await asyncio.to_thread(creds.refresh, Request())
                # After refresh, update stored scopes if they changed (Google may grant additional scopes)
                refreshed_scopes = creds.scopes or []
                if refreshed_scopes != scopes:
                    scopes_str = " ".join(refreshed_scopes)
                    await upsert_google_token(
                        db,
                        user_id=user_id,
                        integration=integration_key,
                        access_token=creds.token,
                        refresh_token=creds.refresh_token,
                        expiry=creds.expiry.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if creds.expiry else None,
                        token_type="Bearer",
                        scopes=scopes_str,
                    )
            except Exception as e:
                # If refresh fails due to scope mismatch, log but don't fail - the token might still work
                logger.warning(f"Token refresh had issues (may be scope-related): {e}")
                raise
            expiry_iso = None
            if creds.expiry:
                expiry_iso = creds.expiry.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            await update_google_token_expiry(db, user_id, integration_key, creds.token, expiry_iso)
        else:
            raise ValueError("Google credentials invalid and no refresh token available")

    return build(service_name, service_version, credentials=creds)


async def build_drive_service_for_user(db: Database, user_id: str):
    """Build a Google Drive v3 service for the given user using stored tokens."""
    return await _build_google_service_for_user(
        db,
        user_id,
        integration="drive",
        missing_scope_message="Google account missing Drive access; please reconnect",
        service_name="drive",
        service_version="v3",
    )


async def build_youtube_service_for_user(db: Database, user_id: str):
    """Build a YouTube Data API client for the given user."""
    return await _build_google_service_for_user(
        db,
        user_id,
        integration="youtube",
        missing_scope_message="Google account missing YouTube access; please reconnect",
        service_name="youtube",
        service_version="v3",
    )
