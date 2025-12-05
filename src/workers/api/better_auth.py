"""Integration helpers for Better Auth."""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from fastapi import HTTPException, Request, status

from .config import settings
from .simple_http import AsyncSimpleClient, HTTPStatusError, RequestError

logger = logging.getLogger(__name__)


def _session_headers(request: Request) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    cookie = request.headers.get("Cookie")
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _extract_identity(result: Mapping[str, Any]) -> Dict[str, Any]:
    session = result.get("session") or {}
    user = result.get("user") or {}
    data = {
        "session": session,
        "user": user,
    }
    user_id = user.get("id") or session.get("userId") or session.get("user_id")
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
    if not headers.get("Authorization") and not headers.get("Cookie"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
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

    if response.status_code == 401:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Better Auth session")
    try:
        response.raise_for_status()
    except HTTPStatusError as exc:
        logger.error("better_auth_http_error", extra={"status": exc.response.status_code})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Better Auth validation failed",
        ) from exc

    try:
        result = response.json()
    except Exception as exc:
        logger.error("better_auth_invalid_json", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Better Auth returned invalid response",
        ) from exc

    return _extract_identity(result)
