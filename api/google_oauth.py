"""
Google OAuth utilities: authorization URL generation, code exchange, and building
per-user Google Drive clients from stored tokens.
"""
from typing import Optional
from datetime import timezone

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from .config import settings
from .database import (
    Database,
    get_google_tokens,
    upsert_google_tokens,
    update_google_tokens_expiry,
)
from core.constants import GOOGLE_DRIVE_SCOPES


def get_google_oauth_url(state: str, redirect_uri: str) -> str:
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
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=GOOGLE_DRIVE_SCOPES,
    )
    flow.redirect_uri = redirect_uri
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes=True,
        state=state,
        prompt="consent",
    )
    return auth_url


async def exchange_google_code(db: Database, user_id: str, code: str, redirect_uri: str) -> None:
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
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=GOOGLE_DRIVE_SCOPES,
    )
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)
    creds: Credentials = flow.credentials
    expiry_iso = None
    if creds.expiry:
        expiry_iso = creds.expiry.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    scopes_str = " ".join(creds.scopes or [])
    await upsert_google_tokens(
        db,
        user_id=user_id,
        access_token=creds.token,
        refresh_token=getattr(creds, "refresh_token", None),
        expiry=expiry_iso,
        token_type="Bearer",
        scopes=scopes_str,
    )


async def build_drive_service_for_user(db: Database, user_id: str):
    """Build a Google Drive v3 service for the given user using stored tokens.
    Refresh tokens if expired and persist updated access token/expiry.
    """
    token_row = await get_google_tokens(db, user_id)
    if not token_row:
        raise ValueError("Google account not linked for this user")

    creds = Credentials(
        token=token_row.get("access_token"),
        refresh_token=token_row.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=GOOGLE_DRIVE_SCOPES,
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            expiry_iso = None
            if creds.expiry:
                expiry_iso = creds.expiry.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            await update_google_tokens_expiry(db, user_id, creds.token, expiry_iso)
        else:
            raise ValueError("Google credentials invalid and no refresh token available")

    service = build("drive", "v3", credentials=creds)
    return service
