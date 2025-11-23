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
from .database import create_job_extended, get_drive_watch_by_channel
from .models import JobType
from .drive_watch import build_channel_token, mark_watch_stopped, update_watch_expiration
from .notifications import notify_activity
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
            "auth": "/auth/github/start",
            "auth_google": "/auth/google/login/start",
            "documents_drive": "/api/v1/documents/drive",
            "optimize": "/api/v1/optimize",
            "generate_blog": "/api/v1/pipelines/generate_blog",
            "jobs": "/api/v1/jobs",
            "health": "/health",
        "docs": "/docs",
        },
    }


def _parse_channel_expiration(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed is None:
            return None
        if not parsed.tzinfo:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat()
    except Exception as exc:
        logger.warning("drive_webhook_expiration_parse_failed", extra={"value": raw, "error": str(exc)})
        return None


@router.post("/drive/webhook", response_class=PlainTextResponse, tags=["Public"])
async def drive_webhook(request: Request):
    channel_id = request.headers.get("X-Goog-Channel-Id")
    resource_id = request.headers.get("X-Goog-Resource-Id")
    token = request.headers.get("X-Goog-Channel-Token")
    if not channel_id or not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing channel headers")
    db = ensure_db()
    watch = await get_drive_watch_by_channel(db, channel_id)
    if not watch:
        logger.warning("drive_webhook_unknown_channel", extra={"channel_id": channel_id})
        return PlainTextResponse("unknown channel", status_code=status.HTTP_202_ACCEPTED)
    watch_user_id = watch.get("user_id")
    document_id = watch.get("document_id")
    if not watch_user_id or not document_id:
        logger.error(
            "drive_webhook_watch_missing_owner",
            extra={"channel_id": channel_id, "document_id": document_id, "user_id": watch_user_id},
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Drive watch metadata missing owner")
    expected = build_channel_token(channel_id, watch_user_id, document_id)
    if token != expected:
        logger.warning(
            "drive_webhook_invalid_token",
            extra={"channel_id": channel_id, "document_id": document_id},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook token")
    if resource_id and resource_id != watch.get("resource_id"):
        logger.warning(
            "drive_webhook_resource_mismatch",
            extra={
                "channel_id": channel_id,
                "document_id": document_id,
                "expected": watch.get("resource_id"),
                "received": resource_id,
            },
        )
    expiration_header = request.headers.get("X-Goog-Channel-Expiration")
    expiration_iso = _parse_channel_expiration(expiration_header)
    if expiration_iso:
        await update_watch_expiration(db, watch_id=watch.get("watch_id"), user_id=watch.get("user_id"), expiration=expiration_iso)
    resource_state = (request.headers.get("X-Goog-Resource-State") or "").lower()
    if resource_state == "sync":
        return PlainTextResponse("sync", status_code=status.HTTP_200_OK)
    if resource_state == "stop":
        await mark_watch_stopped(db, channel_id)
        return PlainTextResponse("stopped", status_code=status.HTTP_200_OK)

    queue = get_queue_producer()
    job_id = str(uuid.uuid4())
    try:
        await create_job_extended(
            db,
            job_id,
            watch_user_id,
            job_type=JobType.DRIVE_CHANGE_POLL.value,
            document_id=document_id,
            payload={"document_ids": [document_id]},
        )
    except Exception as exc:
        logger.error(
            "drive_webhook_job_create_failed",
            exc_info=True,
            extra={"channel_id": channel_id, "document_id": document_id},
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to enqueue drive poll") from exc

    message = {
        "job_id": job_id,
        "user_id": watch_user_id,
        "job_type": JobType.DRIVE_CHANGE_POLL.value,
        "document_ids": [document_id],
    }
    try:
        await notify_activity(
            db,
            watch_user_id,
            "info",
            "Drive edit detected; syncing document",
            context={
                "document_id": document_id,
                "job_id": job_id,
            },
        )
    except Exception:
        logger.warning(
            "drive_webhook_notify_failed",
            exc_info=True,
            extra={"channel_id": channel_id, "document_id": document_id},
        )
    if not queue:
        logger.error("drive_webhook_queue_missing", extra={"channel_id": channel_id})
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Queue not configured")
    try:
        await queue.send_generic(message)
    except Exception as exc:
        logger.error(
            "drive_webhook_enqueue_failed",
            exc_info=True,
            extra={"channel_id": channel_id, "document_id": document_id},
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Queue unavailable") from exc
    return PlainTextResponse("queued", status_code=status.HTTP_202_ACCEPTED)


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


@router.get("/auth/github/start", tags=["Authentication"]) 
async def github_auth_start(request: Request):
    try:
        auth_url, state = _get_github_oauth_redirect(request)
        return _build_github_oauth_response(request, auth_url, state)
    except Exception as exc:
        logger.exception("GitHub auth initiation failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="GitHub OAuth not configured") from exc


@router.post("/auth/github/start", tags=["Authentication"]) 
async def github_auth_start_post(request: Request, csrf_token: str = Form(...)):
    """POST version of GitHub OAuth start - same as GET but with CSRF validation."""
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or not secrets.compare_digest(cookie_token, csrf_token):
        try:
            client_host = request.client.host if request.client else "-"
            ua = request.headers.get("user-agent", "-")
            logger.warning(
                f"CSRF validation failed: ip={client_host} method={request.method} path={request.url.path} ua={ua} reason=missing or mismatched CSRF token"
            )
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    # Use the same logic as GET endpoint
    return await github_auth_start(request)


@router.get("/auth/google/login/start", tags=["Authentication"])
async def google_login_start(request: Request):
    # Check OAuth configuration early to provide better error message
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured. Please contact support."
        )
    
    try:
        auth_url, state = _get_google_login_oauth_redirect(request)
        return _build_google_oauth_response(request, auth_url, state)
    except (ValueError, AuthenticationError) as exc:
        # ValueError or AuthenticationError when OAuth is not configured
        if "Google OAuth not configured" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google OAuth is not configured. Please contact support."
            ) from exc
        raise
    except Exception as exc:
        logger.exception("Google auth initiation failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Google OAuth not configured") from exc


@router.post("/auth/google/login/start", tags=["Authentication"])
async def google_login_start_post(request: Request, csrf_token: str = Form(...)):
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or not secrets.compare_digest(cookie_token, csrf_token):
        try:
            client_host = request.client.host if request.client else "-"
            ua = request.headers.get("user-agent", "-")
            logger.warning(
                f"Google login CSRF validation failed: ip={client_host} method={request.method} path={request.url.path} ua={ua}"
            )
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    # Use the same logic as GET endpoint (sessions instead of cookies)
    return await google_login_start(request)


@router.get("/auth/set-cookie", tags=["Authentication"])
async def set_auth_cookie(
    session_id: str,
    redirect: str = "/dashboard", 
    request: Request = None
):
    """
    Dedicated endpoint to set the access_token cookie.
    This is called after OAuth callback to set the cookie on a same-site request.
    
    Receives session_id which contains the JWT token in the session's extra field.
    
    Security: Validates redirect parameter to prevent open redirect attacks.
    Only relative paths (starting with "/") are allowed.
    """
    from .auth import verify_jwt_token
    from .config import settings
    from .database import get_user_session, delete_user_session
    import json
    
    if not session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="session_id parameter required")
    
    # Validate redirect to prevent open redirect attacks
    # Only allow relative paths (starting with "/") and reject protocol-relative URLs ("//")
    if not redirect.startswith("/"):
        logger.warning("set_auth_cookie: Rejected non-relative redirect: %s", redirect)
        redirect = "/dashboard"
    if redirect.startswith("//"):
        # URLs like "//evil.com" are protocol-relative and could redirect externally
        logger.warning("set_auth_cookie: Rejected protocol-relative redirect: %s", redirect)
        redirect = "/dashboard"
    
    # Get token from session
    db = ensure_db()
    session = await get_user_session(db, session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Session not found or expired")
    
    extra = json.loads(session.get("extra", "{}")) if isinstance(session.get("extra"), str) else (session.get("extra") or {})
    jwt_token = extra.get("jwt_token")
    
    if not jwt_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JWT token not found in session")
    
    # Delete the temp session after retrieving token
    await delete_user_session(db, session_id)
    logger.info("set_auth_cookie: Retrieved token from session %s", session_id)
    
    # Verify the token is valid before setting cookie
    try:
        payload = verify_jwt_token(jwt_token)
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token")
    except Exception as exc:
        logger.warning("set_auth_cookie: Invalid token provided: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token") from exc
    
    is_secure = is_secure_request(request)
    max_age_seconds = settings.jwt_expiration_hours * 3600
    
    logger.info(
        "set_auth_cookie: Setting access_token cookie: secure=%s, max_age=%d, redirect=%s, method=%s",
        is_secure,
        max_age_seconds,
        redirect,
        "session" if session_id else "query_string",
    )
    
    # Set cookie and redirect
    response = RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key="access_token",
        value=jwt_token,
        httponly=True,
        secure=is_secure,
        samesite="lax",  # Will work since this is same-site navigation
        max_age=max_age_seconds,
        path="/",
    )
    
    set_cookie_headers = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
    logger.info("set_auth_cookie: Set-Cookie headers: %s", set_cookie_headers)
    
    return response


@router.get("/auth/github/callback", tags=["Authentication"])
async def github_callback(code: str, state: str, request: Request):
    stored_state = request.cookies.get(COOKIE_OAUTH_STATE)
    if not stored_state or not secrets.compare_digest(stored_state, state):
        logger.warning("GitHub login state verification failed - possible CSRF attack")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid state parameter - possible CSRF attack")

    # Skip DB check - we're not using DB for auth anymore
    try:
        db = ensure_db()
    except HTTPException as exc:
        if exc.status_code == 500:
            logger.error("GitHub OAuth callback: Database unavailable - cannot complete authentication")
            is_secure = is_secure_request(request)
            response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
            response.delete_cookie(key=COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
        raise

    try:
        # Generate JWT directly from OAuth data without DB persistence
        # This works in Workers without requiring D1 database
        # Get redirect_uri to match authorization request (RFC 6749 compliance)
        redirect_uri = str(request.url.replace(path="/auth/github/callback", query=""))
        token_data = await exchange_github_code(code, redirect_uri=redirect_uri)
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No access token received from GitHub")
        
        github_user = await get_github_user_info(access_token)
        raw_github_id = github_user.get("id")
        if raw_github_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="GitHub user id missing")
        github_id = str(raw_github_id)
        email = github_user.get("email") or await get_github_primary_email(access_token)
        if not email:
            username = github_user.get("login", "github")
            email = f"{username}_{github_id}@users.noreply.github.com"
        
        user_id = f"github_{github_id}"
        from .auth import generate_jwt_token
        from .database import create_user
        
        # Create or update user in database (required for foreign key constraint in user_sessions)
        # create_user may return an existing user if github_id already exists for a different user_id
        db = ensure_db()
        created_user = await create_user(db, user_id, github_id=github_id, email=email)
        
        # Use the actual user_id from the returned user (might be different if user already existed)
        actual_user_id = created_user.get("user_id") or user_id
        actual_github_id = created_user.get("github_id") or github_id
        actual_email = created_user.get("email") or email
        
        jwt_token = generate_jwt_token(actual_user_id, github_id=actual_github_id, email=actual_email)
        user_response = {"user_id": actual_user_id, "email": actual_email, "github_id": actual_github_id}

        is_secure = is_secure_request(request)
        if settings.jwt_use_cookies:
            max_age_seconds = settings.jwt_expiration_hours * 3600
            logger.info(
                "GitHub OAuth: Setting access_token cookie: secure=%s, max_age=%d, path=/, jwt_length=%d, user_id=%s, scheme=%s, host=%s",
                is_secure,
                max_age_seconds,
                len(jwt_token),
                user_id,
                request.url.scheme,
                request.headers.get("host"),
            )
            # Use sessions: create session with JWT in extra, then redirect to set-cookie endpoint
            # This uses existing session infrastructure - no query string fallback
            session_id = secrets.token_urlsafe(32)
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)  # Short-lived temp session
            await create_user_session(
                db,
                session_id,
                actual_user_id,  # Use actual_user_id from create_user, not calculated user_id
                expires_at,
                ip_address=(request.client.host if request.client else None),
                user_agent=request.headers.get("user-agent"),
                extra={"jwt_token": jwt_token, "temp": True, "provider": "github"},
            )
            # Redirect to set-cookie endpoint with session_id
            cookie_set_url = f"/auth/set-cookie?session_id={session_id}&redirect=/dashboard"
            response = RedirectResponse(url=cookie_set_url, status_code=status.HTTP_302_FOUND)
            logger.info("GitHub OAuth: Created temp session %s, redirecting to set-cookie", session_id)
            # Log the actual Set-Cookie header to verify it's being set
            set_cookie_headers = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
            logger.info(
                "GitHub OAuth: Response Set-Cookie headers (count=%d): %s",
                len(set_cookie_headers),
                set_cookie_headers,
            )
            # Also log all response headers for debugging
            logger.debug(
                "GitHub OAuth: All response headers: %s",
                dict(response.headers),
            )
            # Session cookies disabled - SessionMiddleware is not enabled
            # await _issue_session_cookie(
            #     response,
            #     request,
            #     db,
            #     user["user_id"],
            #     is_secure=is_secure,
            #     provider="github",
            # )
            # OAuth state was stored in session and already deleted when retrieved
            
            # Log final headers before returning
            final_set_cookie_headers = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
            logger.info(
                "GitHub OAuth: Final Set-Cookie headers after state deletion (count=%d): %s",
                len(final_set_cookie_headers),
                final_set_cookie_headers,
            )
            return response
        else:
            response = JSONResponse(content={"access_token": jwt_token, "token_type": "bearer", "user": user_response})
            return response
    except Exception:
        logger.exception("GitHub callback failed")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication failed")


@router.get("/auth/google/login/callback", tags=["Authentication"])
async def google_login_callback(code: str, state: str, request: Request):
    stored_state = request.cookies.get(COOKIE_GOOGLE_OAUTH_STATE)
    if not stored_state or not secrets.compare_digest(stored_state, state):
        logger.warning("Google login state verification failed - possible CSRF attack")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid state parameter - possible CSRF attack")

    # Skip DB check - we're not using DB for auth anymore
    try:
        db = ensure_db()
    except HTTPException as exc:
        if exc.status_code == 500:
            logger.error("Google login callback: Database unavailable - cannot complete authentication")
            is_secure = is_secure_request(request)
            response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
            response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
        raise

    try:
        # Generate JWT directly from OAuth data without DB persistence
        # This works in Workers without requiring D1 database
        redirect_uri = _google_login_redirect_uri(request)
        token_data = await exchange_google_login_code(code, redirect_uri)
        id_token_value = token_data.get("id_token")
        if not id_token_value:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No ID token received from Google")
        
        id_info = await _verify_google_id_token(id_token_value)
        raw_google_id = id_info.get("sub")
        if not raw_google_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google user id missing")
        google_id = str(raw_google_id)
        
        email = None
        if id_info.get("email") and (id_info.get("email_verified") is True):
            email = id_info.get("email")
        
        # Try userinfo endpoint if email not in ID token
        if not email:
            access_token = token_data.get("access_token")
            if access_token:
                try:
                    userinfo = await get_google_user_info(access_token)
                    if userinfo.get("email") and (userinfo.get("email_verified") is True):
                        email = userinfo.get("email")
                except Exception:
                    pass
        
        if not email:
            email = f"google_user_{google_id}@accounts.google.com"
        
        user_id = f"google_{google_id}"
        from .database import create_user
        
        # Create or update user in database (required for foreign key constraint in user_sessions)
        # create_user may return an existing user if google_id already exists for a different user_id
        db = ensure_db()
        created_user = await create_user(db, user_id, google_id=google_id, email=email)
        
        # Use the actual user_id from the returned user (might be different if user already existed)
        actual_user_id = created_user.get("user_id") or user_id
        actual_google_id = created_user.get("google_id") or google_id
        actual_email = created_user.get("email") or email
        
        jwt_token = generate_jwt_token(actual_user_id, google_id=actual_google_id, email=actual_email)
        user_response = {
            "user_id": actual_user_id,
            "email": actual_email,
            "google_id": actual_google_id,
        }

        is_secure = is_secure_request(request)
        logger.info(
            "Google OAuth callback: jwt_use_cookies=%s, is_secure=%s, user_id=%s, email=%s",
            settings.jwt_use_cookies,
            is_secure,
            user_id,
            email,
        )
        if settings.jwt_use_cookies:
            max_age_seconds = settings.jwt_expiration_hours * 3600
            logger.info(
                "Google OAuth: Setting access_token cookie: secure=%s, max_age=%d, path=/, jwt_length=%d, user_id=%s, scheme=%s, host=%s",
                is_secure,
                max_age_seconds,
                len(jwt_token),
                user_id,
                request.url.scheme,
                request.headers.get("host"),
            )
            try:
                # Use sessions: create session with JWT in extra, then redirect to set-cookie endpoint
                # This uses existing session infrastructure - no query string fallback
                session_id = secrets.token_urlsafe(32)
                expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)  # Short-lived temp session
                await create_user_session(
                    db,
                    session_id,
                    actual_user_id,  # Use actual_user_id from create_user, not calculated user_id
                    expires_at,
                    ip_address=(request.client.host if request.client else None),
                    user_agent=request.headers.get("user-agent"),
                    extra={"jwt_token": jwt_token, "temp": True, "provider": "google"},
                )
                # Redirect to set-cookie endpoint with session_id
                cookie_set_url = f"/auth/set-cookie?session_id={session_id}&redirect=/dashboard"
                response = RedirectResponse(url=cookie_set_url, status_code=status.HTTP_302_FOUND)
                logger.info("Google OAuth: Created temp session %s, redirecting to set-cookie", session_id)
                # Log the actual Set-Cookie header to verify it's being set
                set_cookie_headers = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
                logger.info(
                    "Google OAuth: Response Set-Cookie headers (count=%d): %s",
                    len(set_cookie_headers),
                    set_cookie_headers,
                )
                # Verify cookie is actually in headers
                if not set_cookie_headers or not any("access_token" in h for h in set_cookie_headers):
                    logger.error(
                        "Google OAuth: WARNING - access_token cookie NOT found in Set-Cookie headers! Headers: %s",
                        dict(response.headers),
                    )
                # Also log all response headers for debugging
                logger.debug(
                    "Google OAuth: All response headers: %s",
                    dict(response.headers),
            )
            # Session cookies disabled - SessionMiddleware is not enabled
            # await _issue_session_cookie(
            #     response,
            #     request,
            #     db,
            #     user["user_id"],
            #     is_secure=is_secure,
            #     provider="google",
            # )
                response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
                logger.info("Google OAuth: Returning redirect response with access_token cookie")
                return response
            except Exception as cookie_exc:
                logger.error(
                    "Google OAuth: Failed to set access_token cookie: %s",
                    cookie_exc,
                    exc_info=True,
                )
                # Cookie setting failed - redirect to login with error
                response = RedirectResponse(url="/login?error=cookie_failed", status_code=status.HTTP_302_FOUND)
                response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
                return response
        else:
            response = JSONResponse(content={"access_token": jwt_token, "token_type": "bearer", "user": user_response})
            response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
    except (ValueError, AuthenticationError) as exc:
        logger.error(
            "Google login callback failed: %s",
            exc,
            exc_info=True,
        )
        # Expected auth errors: clear state cookie and return safe message
        logger.warning("Google callback failed: %s", exc)
        is_secure = is_secure_request(request)
        message = str(exc)
        if settings.jwt_use_cookies:
            response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        else:
            response = JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": message})
        response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
        return response
    except Exception:
        # Unexpected errors: log with stack, clear state cookie, return generic message
        logger.exception("Google callback failed")
        is_secure = is_secure_request(request)
        if settings.jwt_use_cookies:
            response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        else:
            response = JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Authentication failed"})
        response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
        return response


@router.get("/auth/logout", tags=["Authentication"])
async def logout_get(request: Request):
    return await _build_logout_response(request)
