from fastapi import APIRouter, Request, HTTPException, status, Form
from fastapi.responses import JSONResponse, RedirectResponse, Response, PlainTextResponse
from typing import Optional
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from .config import settings
from .constants import COOKIE_OAUTH_STATE, COOKIE_GOOGLE_OAUTH_STATE
from .utils import is_secure_request
from .auth import (
    authenticate_github,
    authenticate_google,
    exchange_github_code,
    get_github_user_info,
    get_github_primary_email,
    exchange_google_login_code,
    get_google_user_info,
    generate_jwt_token,
    _verify_google_id_token,
)
from .exceptions import AuthenticationError
from .deps import ensure_db, get_queue_producer, get_current_user
from .database import create_user_session, delete_user_session
from .app_logging import get_logger
from .models import JobType
from .simple_http import AsyncSimpleClient, HTTPStatusError, RequestError

router = APIRouter()

logger = get_logger(__name__)


# Removed _is_secure_request - now using shared is_secure_request from utils


def _get_github_oauth_redirect(request: Request) -> tuple[str, str]:
    """Get GitHub OAuth authorization URL and state.
    
    Uses request.url for redirect_uri instead of BASE_URL to ensure it matches
    what GitHub redirects to. This is important for:
    - Development: request.url is the actual localhost URL
    - Production behind proxy: BASE_URL may be set, but request.url ensures consistency
    
    Note: In production behind a proxy/load balancer, BASE_URL could be used for consistency,
    but using request.url ensures the redirect_uri always matches the actual request origin,
    which is critical for OAuth security validation.
    """
    # Always use the actual request URL for redirect_uri to ensure it matches what GitHub redirects to
    # BASE_URL is only for production behind proxy/load balancer - in dev, use actual request URL
    redirect_uri = str(request.url.replace(path="/auth/github/callback", query=""))
    logger.debug(f"Using request URL for GitHub OAuth redirect_uri: {redirect_uri}")
    from . import auth as auth_module
    return auth_module.get_github_oauth_url(redirect_uri)


def _build_github_oauth_response(request: Request, auth_url: str, state: str) -> RedirectResponse:
    is_secure = is_secure_request(request)
    response = RedirectResponse(url=auth_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=COOKIE_OAUTH_STATE,
        value=state,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=600,
        path="/",
    )
    return response


def _google_login_redirect_uri(request: Request) -> str:
    """Get Google OAuth redirect URI.
    
    Uses request.url for redirect_uri instead of BASE_URL to ensure it matches
    what Google redirects to. This is important for:
    - Development: request.url is the actual localhost URL
    - Production behind proxy: BASE_URL may be set, but request.url ensures consistency
    
    Note: In production behind a proxy/load balancer, BASE_URL could be used for consistency,
    but using request.url ensures the redirect_uri always matches the actual request origin,
    which is critical for OAuth security validation.
    """
    # Always use the actual request URL for redirect_uri to ensure it matches what Google redirects to
    # BASE_URL is only for production behind proxy/load balancer - in dev, use actual request URL
    redirect_uri = str(request.url.replace(path="/auth/google/login/callback", query=""))
    logger.debug(f"Using request URL for Google login redirect_uri: {redirect_uri}")
    return redirect_uri


def _get_google_login_oauth_redirect(request: Request) -> tuple[str, str]:
    redirect_uri = _google_login_redirect_uri(request)
    from . import auth as auth_module
    return auth_module.get_google_login_oauth_url(redirect_uri)


def _build_google_oauth_response(request: Request, auth_url: str, state: str) -> RedirectResponse:
    is_secure = is_secure_request(request)
    response = RedirectResponse(url=auth_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=COOKIE_GOOGLE_OAUTH_STATE,
        value=state,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=600,
        path="/",
    )
    return response


async def _issue_session_cookie(
    response: Response,
    request: Request,
    db,
    user_id: str,
    *,
    is_secure: bool,
    provider: str,
) -> None:
    # Validate session_ttl_hours type and range
    if not isinstance(settings.session_ttl_hours, (int, float)) or settings.session_ttl_hours <= 0:
        logger.warning("Invalid session_ttl_hours: %s", settings.session_ttl_hours)
        return
    # Guard against extremely large values that could cause integer overflow
    # Max safe value: ~596,523 hours (2147483647 seconds / 3600) for 32-bit systems
    if settings.session_ttl_hours > 500000:
        logger.warning("session_ttl_hours exceeds safe maximum: %s", settings.session_ttl_hours)
        return
    ttl_seconds = int(settings.session_ttl_hours * 3600)
    if ttl_seconds <= 0:
        return
    session_id = secrets.token_urlsafe(32)
    try:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
        await create_user_session(
            db,
            session_id,
            user_id,
            expires_at,
            ip_address=(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent"),
            extra={"provider": provider},
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to persist browser session for user %s: %s", user_id, exc)
        return
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=ttl_seconds,
        path="/",
    )


async def _build_logout_response(request: Request, *, redirect: str = "/") -> RedirectResponse:
    response = RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)
    is_secure = is_secure_request(request)
    response.delete_cookie("access_token", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie(COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie(COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_redirect_uri", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_integration", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_redirect_next", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("csrf_token", path="/", samesite="lax", httponly=True, secure=is_secure)
    session_cookie = request.cookies.get(settings.session_cookie_name)
    if session_cookie:
        try:
            db = ensure_db()
            # Get user_id from session if available for ownership validation
            user_id = None
            session = getattr(request.state, "session", None)
            if session:
                user_id = session.get("user_id")
            await delete_user_session(db, session_cookie, user_id=user_id)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("Failed to revoke browser session during logout: %s", exc)
        response.delete_cookie(
            settings.session_cookie_name,
            path="/",
            samesite="lax",
            httponly=True,
            secure=is_secure,
        )
    return response


@router.get("/api", tags=["Public"])
async def root():
    # Determine queue mode for debugging
    queue_mode = "inline" if settings.use_inline_queue else ("workers-binding" if settings.queue else ("api" if settings.cloudflare_api_token else "none"))
    
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "queue_mode": queue_mode,
        "endpoints": {
            "jobs": "/api/v1/jobs",
            "health": "/health",
            "docs": "/docs",
        },
    }


@router.get("/robots.txt", response_class=PlainTextResponse, tags=["Public"])
async def robots_txt(request: Request) -> str:
    base = (settings.base_url or "").strip().rstrip("/")
    if not base:
        base = f"{request.url.scheme}://{request.url.netloc}"
    return f"""User-agent: *
Allow: /

Sitemap: {base}/sitemap.xml
"""


@router.get("/health", tags=["Public"]) 
async def health():
    # Minimal health; deeper checks can live in protected ops if needed
    return {
        "status": "healthy",
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc),
    }


@router.get("/auth/logout", tags=["Authentication"], include_in_schema=False)
async def logout_get(request: Request):
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="Browser-based authentication is no longer supported")
