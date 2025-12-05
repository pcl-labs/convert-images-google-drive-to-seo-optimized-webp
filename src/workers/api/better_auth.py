"""Integration helpers for Better Auth."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from fastapi import HTTPException, Request, status

from .config import settings
from .simple_http import AsyncSimpleClient, HTTPStatusError, RequestError

logger = logging.getLogger(__name__)

REQUIRED_YOUTUBE_SCOPES = {
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
}


@dataclass
class YouTubeIntegration:
    integration_id: str
    organization_id: Optional[str]
    access_token: Optional[str]
    refresh_token: Optional[str]
    expires_at: Optional[datetime]
    scopes: Sequence[str]


def _session_headers(request: Request) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    cookie = request.headers.get("Cookie")
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _parse_datetime(value: Optional[Any]) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_scopes(raw: Any) -> Sequence[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(scope).strip() for scope in raw if str(scope).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            import json

            decoded = json.loads(text)
            if isinstance(decoded, list):
                return [str(scope).strip() for scope in decoded if str(scope).strip()]
        except Exception:
            pass
    parts = [part.strip() for part in text.replace(",", " ").split()]
    return [part for part in parts if part]


def _has_required_scope(scopes: Sequence[str]) -> bool:
    normalized = {scope.strip().lower() for scope in scopes if scope}
    for required in REQUIRED_YOUTUBE_SCOPES:
        if required.lower() in normalized:
            return True
    return False


def _extract_identity(result: Mapping[str, Any]) -> Dict[str, Any]:
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Better Auth response is None",
        )
    session = result.get("session") or {}
    user = result.get("user") or {}
    data = {
        "session": session,
        "user": user,
    }
    user_id = user.get("id") or session.get("userId") or session.get("user_id")
    # Anonymous users should still have a user_id from Better Auth
    # If no user_id, this is an invalid session response
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Better Auth session did not include a user identifier",
        )
    data["user_id"] = user_id
    data["session_id"] = session.get("id")
    data["organization_id"] = (
        session.get("organizationId")
        or session.get("organization_id")
        or user.get("organizationId")
        or user.get("organization_id")
    )
    data["role"] = session.get("role") or user.get("role")
    return data


async def authenticate_with_better_auth(request: Request) -> Dict[str, Any]:
    """Validate the current request with Better Auth."""
    base_url = settings.better_auth_base_url
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="BETTER_AUTH_BASE_URL is not configured",
        )
    headers = _session_headers(request)
    # Don't require Authorization/Cookie - we'll create anonymous session if needed
    endpoint = settings.better_auth_session_endpoint or "/api/auth/get-session"
    url = endpoint if endpoint.startswith("http") else f"{base_url.rstrip('/')}{endpoint}"
    try:
        async with AsyncSimpleClient(timeout=settings.better_auth_timeout_seconds) as client:
            response = await client.get(url, headers=headers)
    except RequestError as exc:
        logger.error("better_auth_network_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to reach Better Auth service",
        ) from exc
    except Exception as exc:
        logger.error("better_auth_unexpected_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error contacting Better Auth",
        ) from exc

    logger.info(
        "better_auth_response",
        extra={
            "status": response.status_code,
            "url": url,
            "has_auth_header": bool(headers.get("Authorization")),
            "has_cookie": bool(headers.get("Cookie")),
            "content_length": len(response.text) if hasattr(response, "text") else 0,
        },
    )

    if response.status_code == 401:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Better Auth session")
    try:
        response.raise_for_status()
    except HTTPStatusError as exc:
        logger.error("better_auth_http_error", extra={"status": exc.response.status_code, "response_text": response.text[:200]})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Better Auth validation failed",
        ) from exc

    try:
        result = response.json()
    except Exception as exc:
        logger.error("better_auth_invalid_json", extra={"status": response.status_code, "response_text": response.text[:200]}, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Better Auth returned invalid response",
        ) from exc

    if result is None:
        # Better Auth returned null (no session) - create an anonymous session
        logger.info(
            "better_auth_no_session_creating_anonymous",
            extra={
                "status": response.status_code,
                "url": url,
            },
        )
        # Create anonymous session by calling sign-in-anonymous endpoint
        anonymous_endpoint = "/api/auth/sign-in-anonymous"
        anonymous_url = anonymous_endpoint if anonymous_endpoint.startswith("http") else f"{base_url.rstrip('/')}{anonymous_endpoint}"
        
        try:
            async with AsyncSimpleClient(timeout=settings.better_auth_timeout_seconds) as client:
                # Call sign-in-anonymous - it may return a session cookie or JSON response
                anonymous_response = await client.post(anonymous_url, headers=headers)
                anonymous_response.raise_for_status()
                anonymous_result = anonymous_response.json()
                
                if anonymous_result and anonymous_result.get("session"):
                    # We got an anonymous session, extract identity from it
                    logger.info("better_auth_anonymous_session_created")
                    return _extract_identity(anonymous_result)
                else:
                    # Anonymous sign-in didn't return a session, proceed with minimal identity
                    logger.warning("better_auth_anonymous_signin_no_session", extra={"response": anonymous_result})
        except Exception as exc:
            logger.warning("better_auth_anonymous_signin_failed", exc_info=True, extra={"error": str(exc)})
            # If anonymous sign-in fails, proceed with minimal identity for rate limiting
            pass
        
        # Fallback: Return minimal identity for rate limiting
        auth_header = headers.get("Authorization", "")
        return {
            "user_id": None,
            "session_id": None,
            "organization_id": None,
            "role": None,
            "session": {},
            "user": {},
            "_auth_header": auth_header[:20] if auth_header else None,  # For rate limiting
        }

    return _extract_identity(result)


async def fetch_youtube_integration(request: Request) -> Tuple[Optional[YouTubeIntegration], bool]:
    """Fetch connected YouTube integration for the current organization.
    
    Returns (integration, forbidden):
        - integration: YouTubeIntegration if available, otherwise None
        - forbidden: True if Better Auth returned 403 (no permission)
    """
    base_url = settings.better_auth_base_url
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="BETTER_AUTH_BASE_URL is not configured",
        )
    headers = _session_headers(request)
    if not headers:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    endpoint = settings.better_auth_integrations_endpoint or "/api/organization/integrations"
    url = endpoint if endpoint.startswith("http") else f"{base_url.rstrip('/')}{endpoint}"
    try:
        async with AsyncSimpleClient(timeout=settings.better_auth_timeout_seconds) as client:
            response = await client.get(url, headers=headers)
    except RequestError as exc:
        logger.error("better_auth_integrations_network_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to fetch integrations from Better Auth",
        ) from exc
    except Exception as exc:
        logger.error("better_auth_integrations_unexpected_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error contacting Better Auth",
        ) from exc

    if response.status_code == 403:
        return None, True
    if response.status_code == 401:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Better Auth session")
    try:
        response.raise_for_status()
    except HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.error(
            "better_auth_integrations_http_error",
            extra={"status": status_code},
        )
        # Treat non-401/403 client errors as absence of integration so the worker can fall back
        if 400 <= status_code < 500:
            return None, False
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch integrations",
        ) from exc

    try:
        integrations = response.json()
    except Exception as exc:
        logger.error("better_auth_integrations_invalid_json", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Integrations response from Better Auth was invalid",
        ) from exc

    if not isinstance(integrations, list):
        return None, False

    best: Optional[YouTubeIntegration] = None
    for entry in integrations:
        if not isinstance(entry, Mapping):
            continue
        if (entry.get("provider") or "").lower() != "youtube":
            continue
        if (entry.get("status") or "").lower() != "connected":
            continue
        scopes = _parse_scopes(entry.get("scopes"))
        if scopes and not _has_required_scope(scopes):
            continue
        best = YouTubeIntegration(
            integration_id=str(entry.get("id")),
            organization_id=entry.get("organizationId") or entry.get("organization_id"),
            access_token=entry.get("accessToken"),
            refresh_token=entry.get("refreshToken"),
            expires_at=_parse_datetime(entry.get("expiresAt")),
            scopes=scopes,
        )
    return best, False


async def refresh_youtube_access_token(refresh_token: str) -> Dict[str, Any]:
    """Refresh a Google OAuth access token using the stored refresh token."""
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing refresh token for YouTube account",
        )
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth credentials not configured for token refresh",
        )
    payload = {
        "refresh_token": refresh_token,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "grant_type": "refresh_token",
    }
    try:
        async with AsyncSimpleClient(timeout=10.0) as client:
            response = await client.post("https://oauth2.googleapis.com/token", data=payload)
    except RequestError as exc:
        logger.error("google_refresh_network_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to refresh YouTube access token",
        ) from exc
    try:
        response.raise_for_status()
    except HTTPStatusError as exc:
        logger.error(
            "google_refresh_http_error",
            extra={"status": exc.response.status_code},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="YouTube token refresh failed",
        ) from exc

    try:
        data = response.json()
    except Exception as exc:
        logger.error("google_refresh_invalid_json", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid response from Google token refresh",
        ) from exc

    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google token refresh response missing access_token",
        )
    expires_in = data.get("expires_in")
    expires_at = None
    if expires_in:
        try:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        except Exception:
            expires_at = None
    data["expires_at"] = expires_at
    return data
