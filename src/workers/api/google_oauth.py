"""Google OAuth helpers that avoid heavy google-api-client dependencies."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

from .simple_http import AsyncSimpleClient, HTTPStatusError, RequestError

from core.constants import GOOGLE_INTEGRATION_SCOPES
from core.google_clients import GoogleDocsClient, GoogleDriveClient, OAuthToken

from .config import settings
from .database import Database, get_google_token, upsert_google_token

logger = logging.getLogger(__name__)

AVAILABLE_GOOGLE_INTEGRATIONS = set(GOOGLE_INTEGRATION_SCOPES.keys())
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"


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


def _parse_expiry(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_google_scope_list(raw_scopes: Optional[object]) -> List[str]:
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
    except (ValueError, json.JSONDecodeError):
        pass
    return [scope for scope in scope_text.replace(",", " ").split() if scope.strip()]


def _scope_text(scopes: Any) -> str:
    if scopes is None:
        return ""
    if isinstance(scopes, str):
        return scopes
    if isinstance(scopes, Iterable):
        parts = [str(scope).strip() for scope in scopes if str(scope).strip()]
        return " ".join(parts)
    return str(scopes)


def get_google_oauth_url(state: str, redirect_uri: str, *, integration: str) -> str:
    if not settings.google_client_id or not settings.google_client_secret:
        raise ValueError("Google OAuth not configured")
    scopes = _scopes_for_integration(integration)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "access_type": "offline",
        "include_granted_scopes": "false",
        "prompt": "consent",
        "state": state,
        "scope": " ".join(scopes),
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def normalize_google_integration(value: Optional[str]) -> str:
    return _normalize_integration(value)


async def _exchange_token(payload: Dict[str, str]) -> Dict[str, Any]:
    try:
        async with AsyncSimpleClient(timeout=10.0) as client:
            response = await client.post(TOKEN_ENDPOINT, data=payload)
            response.raise_for_status()
    except HTTPStatusError as exc:
        logger.error(
            "Google token endpoint error",
            extra={"status": exc.response.status_code, "endpoint": TOKEN_ENDPOINT},
        )
        raise ValueError(f"Failed to fetch Google tokens: HTTP {exc.response.status_code}") from exc
    except RequestError as exc:
        logger.error("Network error talking to Google token endpoint: %s", exc)
        raise ValueError("Failed to reach Google OAuth") from exc

    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        status = getattr(response, "status_code", "unknown")
        logger.error(
            "Unable to parse Google token response",
            extra={"status": status},
        )
        raise ValueError("Invalid response from Google OAuth") from exc


async def exchange_google_code(
    db: Database,
    user_id: str,
    code: str,
    redirect_uri: str,
    *,
    integration: str,
) -> None:
    if not settings.google_client_id or not settings.google_client_secret:
        raise ValueError("Google OAuth not configured")
    integration_key = _normalize_integration(integration)
    token_json = await _exchange_token(
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    expiry = None
    expires_in = token_json.get("expires_in")
    try:
        if expires_in is not None:
            expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    except (ValueError, TypeError):
        expiry = None
    scope_text = _scope_text(token_json.get("scope"))
    await upsert_google_token(
        db,
        user_id=user_id,
        integration=integration_key,
        access_token=token_json.get("access_token"),
        refresh_token=token_json.get("refresh_token"),
        expiry=expiry.isoformat().replace("+00:00", "Z") if expiry else None,
        token_type=token_json.get("token_type", "Bearer"),
        scopes=scope_text,
    )


async def _refresh_access_token(
    db: Database,
    user_id: str,
    integration: str,
    refresh_token: str,
) -> Tuple[OAuthToken, List[str]]:
    if not settings.google_client_id or not settings.google_client_secret:
        raise ValueError("Google OAuth not configured")
    token_json = await _exchange_token(
        {
            "refresh_token": refresh_token,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "grant_type": "refresh_token",
        }
    )
    expiry = None
    expires_in = token_json.get("expires_in")
    try:
        if expires_in is not None:
            expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    except (ValueError, TypeError):
        expiry = None
    scope_text = _scope_text(token_json.get("scope"))
    await upsert_google_token(
        db,
        user_id=user_id,
        integration=integration,
        access_token=token_json.get("access_token"),
        refresh_token=refresh_token,
        expiry=expiry.isoformat().replace("+00:00", "Z") if expiry else None,
        token_type=token_json.get("token_type", "Bearer"),
        scopes=scope_text,
    )
    granted_scopes = scope_text.split()
    token = OAuthToken(
        access_token=token_json.get("access_token"),
        refresh_token=refresh_token,
        expiry=expiry,
        token_type=token_json.get("token_type", "Bearer"),
    )
    return token, granted_scopes


async def _load_token_for_user(
    db: Database,
    user_id: str,
    integration: str,
) -> Tuple[OAuthToken, List[str]]:
    integration_key = _normalize_integration(integration)
    token_row = await get_google_token(db, user_id, integration_key)
    if not token_row:
        raise ValueError("Google account not linked for this integration")
    scopes = parse_google_scope_list(token_row.get("scopes"))
    required = _scopes_for_integration(integration_key)
    if not all(scope in scopes for scope in required):
        raise ValueError("Google account missing required scopes; reconnect integration")
    expiry = _parse_expiry(token_row.get("expiry"))
    token = OAuthToken(
        access_token=token_row.get("access_token"),
        refresh_token=token_row.get("refresh_token"),
        expiry=expiry,
        token_type=token_row.get("token_type", "Bearer"),
    )
    now = datetime.now(timezone.utc)
    if token.expiry and token.expiry <= now + timedelta(seconds=60):
        if not token.refresh_token:
            raise ValueError("Google credentials expired and cannot be refreshed")
        token, scopes = await _refresh_access_token(db, user_id, integration_key, token.refresh_token)
    return token, scopes


async def build_drive_service_for_user(db: Database, user_id: str) -> GoogleDriveClient:
    token, _ = await _load_token_for_user(db, user_id, "drive")
    return GoogleDriveClient(token)


async def build_docs_service_for_user(db: Database, user_id: str) -> GoogleDocsClient:
    """Return a lightweight Google Docs client using the Drive integration token."""
    token, _ = await _load_token_for_user(db, user_id, "drive")
    return GoogleDocsClient(token)


__all__ = [
    "build_drive_service_for_user",
    "build_docs_service_for_user",
    "exchange_google_code",
    "get_google_oauth_url",
    "normalize_google_integration",
    "parse_google_scope_list",
]
