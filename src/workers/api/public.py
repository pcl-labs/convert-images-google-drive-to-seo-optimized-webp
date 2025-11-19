from fastapi import APIRouter, Request, HTTPException, status, Form
from fastapi.responses import JSONResponse, RedirectResponse, Response, PlainTextResponse
from typing import Optional
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from .config import settings
from .constants import COOKIE_OAUTH_STATE, COOKIE_GOOGLE_OAUTH_STATE
from .auth import authenticate_github
from .auth import authenticate_google
from .exceptions import AuthenticationError
from .deps import ensure_db, get_queue_producer
from .database import create_user_session, delete_user_session
from .app_logging import get_logger
from .database import create_job_extended, get_drive_watch_by_channel
from .models import JobType
from .drive_watch import build_channel_token, mark_watch_stopped, update_watch_expiration
from .notifications import notify_activity
from .simple_http import AsyncSimpleClient, HTTPStatusError, RequestError

router = APIRouter()

logger = get_logger(__name__)


def _is_secure_request(request: Request) -> bool:
    xf_proto = request.headers.get("x-forwarded-proto", "").lower()
    return (xf_proto == "https") if xf_proto else (request.url.scheme == "https")


def _get_github_oauth_redirect(request: Request) -> tuple[str, str]:
    if settings.base_url and settings.base_url.strip():
        stripped_base = settings.base_url.strip()
        redirect_uri = f"{stripped_base.rstrip('/')}/auth/github/callback"
        logger.debug(f"Using base_url from settings for GitHub OAuth: {stripped_base} -> redirect_uri: {redirect_uri}")
    else:
        redirect_uri = str(request.url.replace(path="/auth/github/callback", query=""))
        logger.debug(f"base_url not set, using request URL for GitHub OAuth -> redirect_uri: {redirect_uri}")
    from . import auth as auth_module
    return auth_module.get_github_oauth_url(redirect_uri)


def _build_github_oauth_response(request: Request, auth_url: str, state: str) -> RedirectResponse:
    is_secure = _is_secure_request(request)
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
    if settings.base_url and settings.base_url.strip():
        base = settings.base_url.strip()
        redirect_uri = f"{base.rstrip('/')}/auth/google/login/callback"
        logger.debug(f"Using base_url from settings for Google login: {base} -> redirect_uri: {redirect_uri}")
        return redirect_uri
    redirect_uri = str(request.url.replace(path="/auth/google/login/callback", query=""))
    logger.debug(f"base_url not set, using request URL for Google login -> redirect_uri: {redirect_uri}")
    return redirect_uri


def _get_google_login_oauth_redirect(request: Request) -> tuple[str, str]:
    redirect_uri = _google_login_redirect_uri(request)
    from . import auth as auth_module
    return auth_module.get_google_login_oauth_url(redirect_uri)


def _build_google_oauth_response(request: Request, auth_url: str, state: str) -> RedirectResponse:
    is_secure = _is_secure_request(request)
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
    is_secure = _is_secure_request(request)
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


@router.get("/test/fetch", tags=["Public"])
async def test_fetch(url: Optional[str] = None, method: Optional[str] = None):
    """
    Test endpoint to verify fetch API works via simple_http.
    Fetches a URL and returns the response status and headers.
    
    Query params:
    - url: URL to fetch (default: https://httpbin.org/get)
    - method: HTTP method (default: GET)
    """
    test_url = url or "https://httpbin.org/get"
    http_method = (method or "GET").upper()
    
    try:
        async with AsyncSimpleClient(timeout=10.0) as client:
            if http_method == "POST":
                response = await client.post(test_url)
            elif http_method == "PUT":
                response = await client.put(test_url)
            elif http_method == "DELETE":
                response = await client.delete(test_url)
            else:
                response = await client.get(test_url)
            
            # Safely get text preview
            try:
                body_preview = response.text[:200] if len(response.text) > 200 else response.text
            except Exception:
                body_preview = f"<binary data, {len(response.content)} bytes>"
            
            return {
                "success": True,
                "url": test_url,
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body_preview": body_preview,
                "body_length": len(response.content),
                "fetch_method": "js.fetch (via AsyncSimpleClient)",
            }
    except HTTPStatusError as exc:
        return {
            "success": False,
            "url": test_url,
            "error": "HTTPStatusError",
            "status_code": exc.response.status_code,
            "message": str(exc),
        }
    except RequestError as exc:
        return {
            "success": False,
            "url": test_url,
            "error": "RequestError",
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "success": False,
            "url": test_url,
            "error": type(exc).__name__,
            "message": str(exc),
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
    except Exception:
        logger.exception("GitHub auth initiation failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="GitHub OAuth not configured")


@router.post("/auth/github/start", tags=["Authentication"]) 
async def github_auth_start_post(request: Request, csrf_token: str = Form(...)):
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
    try:
        auth_url, state = _get_github_oauth_redirect(request)
        return _build_github_oauth_response(request, auth_url, state)
    except Exception:
        logger.exception("GitHub auth initiation (POST) failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="GitHub OAuth not configured")


@router.get("/auth/google/login/start", tags=["Authentication"])
async def google_login_start(request: Request):
    try:
        auth_url, state = _get_google_login_oauth_redirect(request)
        return _build_google_oauth_response(request, auth_url, state)
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
    try:
        auth_url, state = _get_google_login_oauth_redirect(request)
        return _build_google_oauth_response(request, auth_url, state)
    except Exception as exc:
        logger.exception("Google auth initiation (POST) failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Google OAuth not configured") from exc


@router.get("/auth/github/callback", tags=["Authentication"])
async def github_callback(code: str, state: str, request: Request):
    stored_state = request.cookies.get(COOKIE_OAUTH_STATE)
    if not stored_state or not secrets.compare_digest(stored_state, state):
        logger.warning("OAuth state verification failed - possible CSRF attack")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid state parameter - possible CSRF attack")

    # Handle DB initialization failures gracefully for public OAuth callback
    try:
        db = ensure_db()
    except HTTPException as exc:
        if exc.status_code == 500:
            logger.error("GitHub OAuth callback: Database unavailable - cannot complete authentication")
            is_secure = _is_secure_request(request)
            response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
            response.delete_cookie(key=COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
        raise

    try:
        jwt_token, user = await authenticate_github(db, code)
        user_response = {"user_id": user["user_id"], "email": user.get("email"), "github_id": user.get("github_id")}

        is_secure = _is_secure_request(request)
        if settings.jwt_use_cookies:
            max_age_seconds = settings.jwt_expiration_hours * 3600
            response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
            response.set_cookie(
                key="access_token",
                value=jwt_token,
                httponly=True,
                secure=is_secure,
                samesite="lax",
                max_age=max_age_seconds,
                path="/",
            )
            await _issue_session_cookie(
                response,
                request,
                db,
                user["user_id"],
                is_secure=is_secure,
                provider="github",
            )
            response.delete_cookie(key=COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
        else:
            response = JSONResponse(content={"access_token": jwt_token, "token_type": "bearer", "user": user_response})
            response.delete_cookie(key=COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
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

    # Handle DB initialization failures gracefully for public OAuth callback
    try:
        db = ensure_db()
    except HTTPException as exc:
        if exc.status_code == 500:
            logger.error("Google login callback: Database unavailable - cannot complete authentication")
            is_secure = _is_secure_request(request)
            response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
            response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
        raise

    try:
        redirect_uri = _google_login_redirect_uri(request)
        jwt_token, user = await authenticate_google(db, code, redirect_uri)
        user_response = {
            "user_id": user["user_id"],
            "email": user.get("email"),
            "github_id": user.get("github_id"),
            "google_id": user.get("google_id"),
        }

        is_secure = _is_secure_request(request)
        if settings.jwt_use_cookies:
            max_age_seconds = settings.jwt_expiration_hours * 3600
            response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
            response.set_cookie(
                key="access_token",
                value=jwt_token,
                httponly=True,
                secure=is_secure,
                samesite="lax",
                max_age=max_age_seconds,
                path="/",
            )
            await _issue_session_cookie(
                response,
                request,
                db,
                user["user_id"],
                is_secure=is_secure,
                provider="google",
            )
            response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
        else:
            response = JSONResponse(content={"access_token": jwt_token, "token_type": "bearer", "user": user_response})
            response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
    except (ValueError, AuthenticationError) as exc:
        # Expected auth errors: clear state cookie and return safe message
        logger.warning("Google callback failed: %s", exc)
        is_secure = _is_secure_request(request)
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
        is_secure = _is_secure_request(request)
        if settings.jwt_use_cookies:
            response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        else:
            response = JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Authentication failed"})
        response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
        return response


@router.get("/auth/logout", tags=["Authentication"])
async def logout_get(request: Request):
    return await _build_logout_response(request)
