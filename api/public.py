from fastapi import APIRouter, Request, HTTPException, status, Form
from fastapi.responses import JSONResponse, RedirectResponse
from typing import Optional
import secrets
from datetime import datetime, timezone

from .config import settings
from .constants import COOKIE_OAUTH_STATE, COOKIE_GOOGLE_OAUTH_STATE
from .auth import authenticate_github
from .deps import ensure_db
from .app_logging import get_logger

router = APIRouter()

logger = get_logger(__name__)


def _get_github_oauth_redirect(request: Request) -> tuple[str, str]:
    if settings.base_url:
        redirect_uri = f"{settings.base_url.rstrip('/')}/auth/github/callback"
    else:
        redirect_uri = str(request.url.replace(path="/auth/github/callback", query=""))
    from . import auth as auth_module
    return auth_module.get_github_oauth_url(redirect_uri)


def _build_github_oauth_response(request: Request, auth_url: str, state: str) -> RedirectResponse:
    xf_proto = request.headers.get("x-forwarded-proto", "").lower()
    is_secure = (xf_proto == "https") if xf_proto else (request.url.scheme == "https")
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


@router.get("/api", tags=["Public"]) 
async def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "endpoints": {
            "auth": "/auth/github/start",
            "optimize": "/api/v1/optimize",
            "jobs": "/api/v1/jobs",
            "health": "/health",
            "docs": "/docs",
        },
    }


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
    except Exception as e:
        logger.error(f"GitHub auth initiation failed: {e}")
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
    except Exception as e:
        logger.error(f"GitHub auth initiation (POST) failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="GitHub OAuth not configured")


@router.get("/auth/github/callback", tags=["Authentication"]) 
async def github_callback(code: str, state: str, request: Request):
    db = ensure_db()

    stored_state = request.cookies.get(COOKIE_OAUTH_STATE)
    if not stored_state or not secrets.compare_digest(stored_state, state):
        logger.warning("OAuth state verification failed - possible CSRF attack")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid state parameter - possible CSRF attack")

    try:
        jwt_token, user = await authenticate_github(db, code)
        user_response = {"user_id": user["user_id"], "email": user.get("email"), "github_id": user.get("github_id")}

        xf_proto = request.headers.get("x-forwarded-proto", "").lower()
        is_secure = (xf_proto == "https") if xf_proto else (request.url.scheme == "https")
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
            response.delete_cookie(key=COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
        else:
            response = JSONResponse(content={"access_token": jwt_token, "token_type": "bearer", "user": user_response})
            response.delete_cookie(key=COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
            return response
    except Exception as e:
        logger.error(f"GitHub callback failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Authentication failed: {str(e)}")


@router.get("/auth/logout", tags=["Authentication"]) 
async def logout_get(request: Request):
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    xf_proto = request.headers.get("x-forwarded-proto", "").lower()
    is_secure = (xf_proto == "https") if xf_proto else (request.url.scheme == "https")
    response.delete_cookie("access_token", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie(COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie(COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_redirect_uri", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("csrf_token", path="/", samesite="lax", httponly=True, secure=is_secure)
    return response
