from fastapi import APIRouter, Request, HTTPException, status, Form
from fastapi.responses import JSONResponse, RedirectResponse, Response, PlainTextResponse
from typing import Optional
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


# Removed _is_secure_request - now using shared is_secure_request from utils


def _get_github_oauth_redirect(request: Request) -> tuple[str, str]:
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


@router.get("/debug/users", tags=["Public"])
async def debug_users():
    """Debug endpoint to list all users in the database."""
    try:
        from .deps import ensure_db
        from .database import _jsproxy_to_list, _jsproxy_to_dict
        
        db = ensure_db()
        users = await db.execute_all("SELECT * FROM users ORDER BY created_at DESC LIMIT 50")
        users_list = _jsproxy_to_list(users)
        users_data = []
        for u in users_list:
            if isinstance(u, dict):
                users_data.append(u)
            else:
                users_data.append(_jsproxy_to_dict(u))
        
        return {
            "success": True,
            "count": len(users_data),
            "users": users_data,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": type(exc).__name__
        }


@router.get("/debug/sessions", tags=["Public"])
async def debug_sessions(session_id: Optional[str] = None):
    """Debug endpoint to list sessions in the database."""
    try:
        from .deps import ensure_db
        from .database import _jsproxy_to_list, _jsproxy_to_dict
        
        db = ensure_db()
        if session_id:
            session = await db.execute(
                "SELECT * FROM user_sessions WHERE session_id = ?",
                (session_id,)
            )
            if not session:
                return {"success": True, "count": 0, "sessions": []}
            session_dict = _jsproxy_to_dict(session)
            return {
                "success": True,
                "count": 1,
                "sessions": [session_dict],
            }
        else:
            sessions = await db.execute_all(
                "SELECT * FROM user_sessions ORDER BY created_at DESC LIMIT 50"
            )
            sessions_list = _jsproxy_to_list(sessions)
            sessions_data = []
            for s in sessions_list:
                if isinstance(s, dict):
                    sessions_data.append(s)
                else:
                    sessions_data.append(_jsproxy_to_dict(s))
            
            return {
                "success": True,
                "count": len(sessions_data),
                "sessions": sessions_data,
            }
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": type(exc).__name__
    }


@router.get("/debug/db-test", tags=["Public"])
async def debug_db_test():
    """
    Debug endpoint to test database access and basic operations.
    Tests D1 connection, schema, and simple queries.
    """
    results = {
        "d1_binding_available": False,
        "ensure_db_success": False,
        "db_error": None,
        "test_queries": {},
    }
    
    # Check if D1 binding is available through settings
    try:
        from .config import settings
        if settings.d1_database:
            results["d1_binding_available"] = True
            results["d1_binding_type"] = str(type(settings.d1_database))
        else:
            results["d1_binding_available"] = False
            results["d1_binding_error"] = "d1_database is None in settings"
    except Exception as exc:
        results["d1_binding_error"] = str(exc)
        results["d1_binding_error_type"] = type(exc).__name__
    
    # Test ensure_db()
    try:
        db = ensure_db()
        results["ensure_db_success"] = True
        results["db_type"] = str(type(db))
        
        # Import helper functions for JsProxy conversion
        from .database import _jsproxy_to_dict, _jsproxy_to_list
        
        # Test a simple query
        try:
            # Try to query a system table or simple SELECT
            result = await db.execute("SELECT 1 as test")
            result_dict = _jsproxy_to_dict(result) if result else None
            results["test_queries"]["select_one"] = {
                "success": True,
                "result": result_dict,
            }
        except Exception as exc:
            results["test_queries"]["select_one"] = {
                "success": False,
                "error": str(exc),
            }
        
        # Test if users table exists
        try:
            result = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            )
            results["test_queries"]["users_table_exists"] = {
                "success": True,
                "exists": result is not None,
            }
        except Exception as exc:
            results["test_queries"]["users_table_exists"] = {
                "success": False,
                "error": str(exc),
            }
        
        # Test listing tables
        try:
            tables = await db.execute_all(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            # Convert JsProxy results to Python list
            tables_list = _jsproxy_to_list(tables)
            # Extract table names
            table_names = []
            for t in tables_list:
                if isinstance(t, dict):
                    table_names.append(t.get("name", ""))
                else:
                    # Try to convert if it's still a JsProxy
                    t_dict = _jsproxy_to_dict(t)
                    table_names.append(t_dict.get("name", ""))
            results["test_queries"]["list_tables"] = {
                "success": True,
                "tables": table_names,
            }
        except Exception as exc:
            results["test_queries"]["list_tables"] = {
                "success": False,
                "error": str(exc),
            }
            
    except Exception as exc:
        results["ensure_db_success"] = False
        results["db_error"] = str(exc)
        results["db_error_type"] = type(exc).__name__
    
    return results


@router.post("/debug/migrate-db", tags=["Public"])
async def debug_migrate_db():
    """
    Debug endpoint to manually trigger database migration.
    Applies the full schema from migrations/schema.sql to D1.
    """
    try:
        from .deps import ensure_db
        from .database import ensure_full_schema
        
        db = ensure_db()
        await ensure_full_schema(db)
        
        # Verify migration by listing tables
        from .database import _jsproxy_to_list
        tables = await db.execute_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables_list = _jsproxy_to_list(tables)
        table_names = []
        for t in tables_list:
            if isinstance(t, dict):
                table_names.append(t.get("name", ""))
            else:
                from .database import _jsproxy_to_dict
                t_dict = _jsproxy_to_dict(t)
                table_names.append(t_dict.get("name", ""))
        
        return {
            "success": True,
            "message": "Database migration completed successfully",
            "tables": table_names,
            "table_count": len(table_names)
        }
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": type(exc).__name__
        }


@router.get("/debug/cookie-test", tags=["Public"])
async def debug_cookie_test(request: Request):
    """
    Debug endpoint to test cookie setting and reading.
    Sets a test cookie and shows what cookies are present in the request.
    """
    # Check what cookies are currently present
    all_cookies = dict(request.cookies)
    
    # Set a test cookie
    response = JSONResponse({
        "message": "Test cookie endpoint",
        "cookies_before": list(all_cookies.keys()),
        "test_cookie_set": True,
    })
    
    # Set a test cookie with same settings as access_token
    is_secure = is_secure_request(request)
    response.set_cookie(
        key="test_cookie",
        value="test_value_12345",
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=3600,
        path="/",
    )
    
    # Also set access_token as a test
    response.set_cookie(
        key="test_access_token",
        value="test_jwt_token_12345",
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=3600,
        path="/",
    )
    
    return response


@router.get("/debug/cookie-redirect-test", tags=["Public"])
async def debug_cookie_redirect_test(request: Request):
    """
    Debug endpoint to test cookie setting on RedirectResponse (like OAuth callback).
    Sets a cookie and redirects to /debug/cookie-read to test if cookie persists.
    """
    is_secure = is_secure_request(request)
    logger.debug(
        "Cookie redirect test: Setting test_access_token cookie: secure=%s, max_age=3600, path=/",
        is_secure,
    )
    
    response = RedirectResponse(url="/debug/cookie-read", status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key="test_access_token",
        value="test_jwt_from_redirect_12345",
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=3600,
        path="/",
    )
    
    logger.debug("Cookie redirect test: Cookie set, redirecting to /debug/cookie-read")
    return response


@router.get("/debug/cookie-read", tags=["Public"])
async def debug_cookie_read(request: Request):
    """
    Debug endpoint to read cookies after they've been set.
    Call /debug/cookie-test first, then this endpoint to see if cookies persist.
    """
    all_cookies = dict(request.cookies)
    return {
        "cookies_present": list(all_cookies.keys()),
        "test_cookie": request.cookies.get("test_cookie"),
        "test_access_token": request.cookies.get("test_access_token"),
        "access_token": request.cookies.get("access_token"),
        "csrf_token": request.cookies.get("csrf_token"),
        "all_cookies": all_cookies,
    }


@router.get("/debug/oauth-callback-test", tags=["Public"])
async def debug_oauth_callback_test(request: Request):
    """
    Debug endpoint to test OAuth callback cookie setting.
    Simulates what happens in the OAuth callback to see if cookies are being set correctly.
    """
    from .auth import generate_jwt_token
    from .config import settings
    
    test_user_id = "debug_test_user"
    test_email = "debug@example.com"
    jwt_token = generate_jwt_token(user_id=test_user_id, email=test_email)
    
    is_secure = is_secure_request(request)
    max_age_seconds = settings.jwt_expiration_hours * 3600
    
    debug_info = {
        "request_info": {
            "scheme": request.url.scheme,
            "host": request.headers.get("host"),
            "is_secure": is_secure,
            "jwt_use_cookies": settings.jwt_use_cookies,
            "jwt_expiration_hours": settings.jwt_expiration_hours,
            "max_age_seconds": max_age_seconds,
        },
        "cookie_settings": {
            "key": "access_token",
            "httponly": True,
            "secure": is_secure,
            "samesite": "lax",
            "max_age": max_age_seconds,
            "path": "/",
        },
        "token_info": {
            "length": len(jwt_token),
            "preview": jwt_token[:50] + "...",
        },
        "current_cookies": dict(request.cookies),
    }
    
    # Create a redirect response like the OAuth callback does
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
    
    # Capture Set-Cookie headers
    set_cookie_headers = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
    debug_info["response_headers"] = {
        "set_cookie_headers": set_cookie_headers,
        "location": response.headers.get("location"),
        "status_code": response.status_code,
    }
    
    # Return debug info as JSON instead of redirect for debugging
    return JSONResponse(content=debug_info)


@router.get("/debug/oauth-callback-simulate", tags=["Public"])
async def debug_oauth_callback_simulate(request: Request):
    """
    Debug endpoint that actually performs a redirect with cookie (like real OAuth callback).
    Use this to test if cookies persist through redirects in your browser.
    """
    from .auth import generate_jwt_token
    from .config import settings
    
    test_user_id = "simulated_oauth_user"
    test_email = "simulated@example.com"
    jwt_token = generate_jwt_token(user_id=test_user_id, email=test_email)
    
    is_secure = is_secure_request(request)
    max_age_seconds = settings.jwt_expiration_hours * 3600
    
    logger.info(
        "Simulated OAuth callback: Setting access_token cookie: secure=%s, max_age=%d, jwt_length=%d",
        is_secure,
        max_age_seconds,
        len(jwt_token),
    )
    
    # Actually redirect like the real OAuth callback
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
    
    # Log headers for debugging
    set_cookie_headers = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
    logger.info("Simulated OAuth callback: Set-Cookie headers: %s", set_cookie_headers)
    
    return response


@router.get("/debug/auto-login", tags=["Public"])
async def debug_auto_login(request: Request):
    """
    Auto-login using stored refresh token - generates JWT and sets cookie.
    Uses the refresh token from .env to get user info and create a session.
    This allows testing without going through OAuth flow each time.
    
    Usage:
    curl -v -L -c cookies.txt http://localhost:8787/debug/auto-login
    curl -b cookies.txt http://localhost:8787/dashboard
    """
    from .auth import generate_jwt_token
    from .config import settings
    from .simple_http import AsyncSimpleClient
    
    # Use refresh token to get user info
    refresh_token = "REMOVED_REFRESH_TOKEN"
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret
    
    if not refresh_token or not client_id or not client_secret:
        return JSONResponse(
            status_code=500,
            content={"error": "Refresh token or client credentials not configured"}
        )
    
    try:
        # Refresh the access token
        async with AsyncSimpleClient(timeout=10.0) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                }
            )
            if response.status_code != 200:
                return JSONResponse(
                    status_code=500,
                    content={"error": f"Token refresh failed: {response.status_code}", "details": response.text[:200]}
                )
            token_data = response.json()
            access_token = token_data.get("access_token")
            
            # Get user info from Google
            userinfo_response = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if userinfo_response.status_code != 200:
                return JSONResponse(
                    status_code=500,
                    content={"error": f"Userinfo failed: {userinfo_response.status_code}"}
                )
            userinfo = userinfo_response.json()
            
            google_id = userinfo.get("id")
            email = userinfo.get("email", f"google_user_{google_id}@accounts.google.com")
            
            if not google_id:
                return JSONResponse(
                    status_code=500,
                    content={"error": "No Google ID in userinfo response"}
                )
            
            user_id = f"google_{google_id}"
            jwt_token = generate_jwt_token(user_id=user_id, google_id=google_id, email=email)
            
            is_secure = is_secure_request(request)
            max_age_seconds = settings.jwt_expiration_hours * 3600
            
            logger.info(
                "Auto-login: Setting access_token cookie: secure=%s, max_age=%d, jwt_length=%d, user_id=%s, email=%s",
                is_secure,
                max_age_seconds,
                len(jwt_token),
                user_id,
                email,
            )
            
            # Set cookie and redirect
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
            
            set_cookie_headers = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
            logger.info("Auto-login: Set-Cookie headers: %s", set_cookie_headers)
            
            return response
            
    except Exception as exc:
        logger.exception("Auto-login failed")
        return JSONResponse(
            status_code=500,
            content={"error": str(exc), "error_type": type(exc).__name__}
        )


@router.get("/debug/simulate-oauth-callback", tags=["Public"])
async def debug_simulate_oauth_callback(request: Request, user_id: Optional[str] = None, email: Optional[str] = None):
    """
    Simulate OAuth callback flow - sets access_token cookie and redirects to dashboard.
    This allows testing cookie behavior without going through actual OAuth flow.
    
    Query params:
    - user_id: User ID (default: test_user_123)
    - email: Email address (default: test@example.com)
    
    Usage:
    curl -v -L -c cookies.txt http://localhost:8787/debug/simulate-oauth-callback
    curl -b cookies.txt http://localhost:8787/dashboard
    """
    from .auth import generate_jwt_token
    from .config import settings
    
    test_user_id = user_id or "test_user_123"
    test_email = email or "test@example.com"
    jwt_token = generate_jwt_token(user_id=test_user_id, email=test_email)
    
    is_secure = is_secure_request(request)
    max_age_seconds = settings.jwt_expiration_hours * 3600
    
    logger.info(
        "Simulated OAuth callback: Setting access_token cookie: secure=%s, max_age=%d, jwt_length=%d, user_id=%s",
        is_secure,
        max_age_seconds,
        len(jwt_token),
        test_user_id,
    )
    
    # Exact same flow as real OAuth callback
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
    
    # Log headers for debugging
    set_cookie_headers = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
    logger.info("Simulated OAuth callback: Set-Cookie headers: %s", set_cookie_headers)
    logger.info("Simulated OAuth callback: All response headers: %s", dict(response.headers))
    
    return response


@router.get("/debug/oauth-callback-debug", tags=["Public"])
async def debug_oauth_callback_debug(request: Request):
    """
    Debug endpoint to check what happens during OAuth callback.
    This endpoint can be used to test cookie setting behavior.
    """
    from .auth import generate_jwt_token
    from .config import settings
    
    # Simulate what happens in OAuth callback
    test_user_id = "debug_oauth_user"
    test_email = "debug_oauth@example.com"
    jwt_token = generate_jwt_token(user_id=test_user_id, email=test_email)
    
    is_secure = is_secure_request(request)
    max_age_seconds = settings.jwt_expiration_hours * 3600
    
    debug_info = {
        "step": "before_cookie_set",
        "request": {
            "scheme": request.url.scheme,
            "host": request.headers.get("host"),
            "cookies_received": dict(request.cookies),
        },
        "settings": {
            "jwt_use_cookies": settings.jwt_use_cookies,
            "is_secure": is_secure,
            "max_age_seconds": max_age_seconds,
        },
        "token": {
            "length": len(jwt_token),
            "preview": jwt_token[:30] + "...",
        },
    }
    
    if settings.jwt_use_cookies:
        # Try to set cookie
        try:
            response = RedirectResponse(url="/debug/cookie-read", status_code=status.HTTP_302_FOUND)
            response.set_cookie(
                key="access_token",
                value=jwt_token,
                httponly=True,
                secure=is_secure,
                samesite="lax",
                max_age=max_age_seconds,
                path="/",
            )
            
            set_cookie_headers = [v for k, v in response.headers.items() if k.lower() == "set-cookie"]
            debug_info["step"] = "after_cookie_set"
            debug_info["response"] = {
                "status_code": response.status_code,
                "location": response.headers.get("location"),
                "set_cookie_headers": set_cookie_headers,
                "all_headers": dict(response.headers),
            }
            debug_info["cookie_set_success"] = any("access_token" in h for h in set_cookie_headers)
            
            # Actually perform redirect so user can see if cookie persists
            return response
        except Exception as exc:
            debug_info["error"] = {
                "message": str(exc),
                "type": type(exc).__name__,
            }
            debug_info["cookie_set_success"] = False
            return JSONResponse(content=debug_info, status_code=500)
    else:
        debug_info["cookie_set_success"] = False
        debug_info["reason"] = "jwt_use_cookies is False"
        return JSONResponse(content=debug_info)


@router.get("/debug/check-auth-config", tags=["Public"])
async def debug_check_auth_config():
    """
    Debug endpoint to check authentication configuration.
    Shows JWT settings, cookie settings, and OAuth configuration.
    """
    from .config import settings
    
    return {
        "jwt_settings": {
            "jwt_use_cookies": settings.jwt_use_cookies,
            "jwt_expiration_hours": settings.jwt_expiration_hours,
            "jwt_algorithm": settings.jwt_algorithm,
            "jwt_secret_key_present": bool(settings.jwt_secret_key),
            "jwt_secret_key_length": len(settings.jwt_secret_key) if settings.jwt_secret_key else 0,
        },
        "oauth_settings": {
            "github_client_id_present": bool(settings.github_client_id),
            "google_client_id_present": bool(settings.google_client_id),
            "google_client_secret_present": bool(settings.google_client_secret),
        },
        "cookie_settings": {
            "session_cookie_name": settings.session_cookie_name,
            "session_ttl_hours": settings.session_ttl_hours,
        },
        "environment": {
            "environment": settings.environment,
            "debug": settings.debug,
            "base_url": settings.base_url,
        },
        "diagnosis": {
            "cookie_should_be_set": settings.jwt_use_cookies,
            "if_cookies_disabled": "Cookies are disabled - tokens will be returned in JSON response body instead",
            "if_cookies_enabled": "Cookies should be set in OAuth callback responses",
        }
    }


@router.get("/debug/generate-test-token", tags=["Public"])
async def debug_generate_test_token(user_id: Optional[str] = None, email: Optional[str] = None):
    """
    Debug endpoint to generate a test JWT token for testing authentication.
    
    Query params:
    - user_id: User ID (default: test_user_123)
    - email: Email address (default: test@example.com)
    
    Returns a JWT token that can be used as access_token cookie.
    """
    from .auth import generate_jwt_token
    
    test_user_id = user_id or "test_user_123"
    test_email = email or "test@example.com"
    
    token = generate_jwt_token(
        user_id=test_user_id,
        email=test_email,
        google_id=f"test_google_{test_user_id}"
    )
    
    return {
        "token": token,
        "user_id": test_user_id,
        "email": test_email,
        "instructions": {
            "curl_test": f'curl -H "Cookie: access_token={token}" http://localhost:8787/dashboard',
            "browser": "Set access_token cookie in browser DevTools with this token value",
            "note": "This is a test token - use OAuth login for real authentication"
        }
    }


@router.post("/debug/google-token", tags=["Public"])
async def debug_google_token(request: Request):
    """
    Debug endpoint to test Google OAuth token functionality.
    
    Accepts a Google OAuth token JSON in the request body and tests:
    - Token validity
    - Token expiry
    - Google Drive API access
    - Simple Drive API call (list root folder)
    
    Example request body:
    {
        "token": "ya29.a0...",
        "refresh_token": "1//0g...",
        "expiry": "2025-11-13T06:56:46.688068Z",
        "scopes": ["https://www.googleapis.com/auth/drive"]
    }
    """
    from datetime import datetime, timezone
    from .simple_http import AsyncSimpleClient, HTTPStatusError, RequestError
    from core.google_clients import GoogleDriveClient, OAuthToken
    
    results = {
        "success": False,
        "token_provided": False,
        "token_info": {},
        "token_validation": {},
        "drive_api_test": {},
        "errors": [],
    }
    
    try:
        # Parse request body
        try:
            body = await request.json()
        except Exception as exc:
            results["errors"].append(f"Failed to parse JSON body: {str(exc)}")
            return results
        
        # Extract token info
        access_token = body.get("token") or body.get("access_token")
        refresh_token = body.get("refresh_token")
        expiry_str = body.get("expiry")
        scopes = body.get("scopes", [])
        client_id = body.get("client_id")
        client_secret = body.get("client_secret")
        
        if not access_token:
            results["errors"].append("No access_token or token provided in request body")
            return results
        
        results["token_provided"] = True
        results["token_info"] = {
            "has_access_token": bool(access_token),
            "has_refresh_token": bool(refresh_token),
            "expiry": expiry_str,
            "scopes": scopes,
            "token_length": len(access_token),
            "token_preview": access_token[:20] + "..." if len(access_token) > 20 else access_token,
        }
        
        # Parse expiry
        expiry = None
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except Exception as exc:
                results["errors"].append(f"Failed to parse expiry: {str(exc)}")
        
        # Check if token is expired
        now = datetime.now(timezone.utc)
        is_expired = False
        expires_in_seconds = None
        if expiry:
            expires_in_seconds = (expiry - now).total_seconds()
            is_expired = expires_in_seconds < 0
        
        results["token_validation"] = {
            "has_expiry": expiry is not None,
            "expiry_datetime": expiry.isoformat() if expiry else None,
            "current_datetime": now.isoformat(),
            "is_expired": is_expired,
            "expires_in_seconds": expires_in_seconds,
            "expires_in_minutes": round(expires_in_seconds / 60, 2) if expires_in_seconds else None,
        }
        
        # Test token with Google OAuth2 tokeninfo endpoint
        tokeninfo_result = {}
        try:
            async with AsyncSimpleClient(timeout=10.0) as client:
                response = await client.get(
                    "https://www.googleapis.com/oauth2/v2/tokeninfo",
                    params={"access_token": access_token}
                )
                if response.status_code == 200:
                    tokeninfo_data = response.json()
                    tokeninfo_result = {
                        "valid": True,
                        "issued_to": tokeninfo_data.get("issued_to"),
                        "audience": tokeninfo_data.get("audience"),
                        "user_id": tokeninfo_data.get("user_id"),
                        "scope": tokeninfo_data.get("scope"),
                        "expires_in": tokeninfo_data.get("expires_in"),
                        "email": tokeninfo_data.get("email"),
                        "verified_email": tokeninfo_data.get("verified_email"),
                    }
                else:
                    tokeninfo_result = {
                        "valid": False,
                        "status_code": response.status_code,
                        "error": response.text[:200] if response.text else "Unknown error",
                    }
        except Exception as exc:
            tokeninfo_result = {
                "valid": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        
        results["token_validation"]["tokeninfo"] = tokeninfo_result
        
        # Test Google Drive API access
        drive_test_result = {}
        try:
            # Create OAuthToken object
            oauth_token = OAuthToken(
                access_token=access_token,
                refresh_token=refresh_token,
                expiry=expiry,
                token_type="Bearer",
            )
            
            # Create Drive client
            drive_client = GoogleDriveClient(oauth_token)
            
            # Test 1: List root folder (simple API call)
            try:
                root_files = drive_client.list_folder_files("root", fields="files(id, name, mimeType)")
                drive_test_result["list_root_folder"] = {
                    "success": True,
                    "file_count": len(root_files.get("files", [])),
                    "files": root_files.get("files", [])[:5],  # First 5 files
                    "has_next_page": bool(root_files.get("nextPageToken")),
                }
            except Exception as exc:
                drive_test_result["list_root_folder"] = {
                    "success": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            
            # Test 2: Get about info (user info)
            try:
                async with AsyncSimpleClient(timeout=10.0) as client:
                    response = await client.get(
                        "https://www.googleapis.com/drive/v3/about",
                        headers={"Authorization": f"Bearer {access_token}"},
                        params={"fields": "user,storageQuota"}
                    )
                    if response.status_code == 200:
                        about_data = response.json()
                        drive_test_result["about"] = {
                            "success": True,
                            "user": about_data.get("user", {}),
                            "storage_quota": about_data.get("storageQuota", {}),
                        }
                    else:
                        drive_test_result["about"] = {
                            "success": False,
                            "status_code": response.status_code,
                            "error": response.text[:200] if response.text else "Unknown error",
                        }
            except Exception as exc:
                drive_test_result["about"] = {
                    "success": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            
        except Exception as exc:
            drive_test_result = {
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        
        results["drive_api_test"] = drive_test_result
        
        # Test token refresh if refresh_token is provided
        refresh_test_result = {}
        if refresh_token and client_id and client_secret:
            try:
                async with AsyncSimpleClient(timeout=10.0) as client:
                    response = await client.post(
                        "https://oauth2.googleapis.com/token",
                        data={
                            "refresh_token": refresh_token,
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "grant_type": "refresh_token",
                        }
                    )
                    if response.status_code == 200:
                        refresh_data = response.json()
                        refresh_test_result = {
                            "success": True,
                            "new_access_token_length": len(refresh_data.get("access_token", "")),
                            "expires_in": refresh_data.get("expires_in"),
                            "token_type": refresh_data.get("token_type"),
                        }
                    else:
                        refresh_test_result = {
                            "success": False,
                            "status_code": response.status_code,
                            "error": response.text[:200] if response.text else "Unknown error",
                        }
            except Exception as exc:
                refresh_test_result = {
                    "success": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            results["token_refresh_test"] = refresh_test_result
        
        results["success"] = True
        
    except Exception as exc:
        results["errors"].append(f"Unexpected error: {str(exc)}")
        results["error_type"] = type(exc).__name__
    
    return results


@router.get("/debug/auth-state", tags=["Public"])
async def debug_auth_state(request: Request):
    """
    Debug endpoint to check authentication state and cookies.
    Shows what cookies are present, what the middleware sees, and JWT token details.
    """
    all_cookies = dict(request.cookies)
    access_token = request.cookies.get("access_token")
    user = getattr(request.state, "user", None)
    
    # Try to decode the token if present
    token_info = None
    if access_token:
        try:
            from .auth import verify_jwt_token
            payload = verify_jwt_token(access_token)
            token_info = {
                "valid": True,
                "user_id": payload.get("user_id"),
                "email": payload.get("email"),
                "github_id": payload.get("github_id"),
                "google_id": payload.get("google_id"),
                "exp": payload.get("exp"),
            }
        except Exception as exc:
            token_info = {
                "valid": False,
                "error": str(exc),
            }
    
    return {
        "cookies_present": list(all_cookies.keys()),
        "access_token": {
            "present": access_token is not None,
            "length": len(access_token) if access_token else 0,
            "preview": access_token[:50] + "..." if access_token and len(access_token) > 50 else access_token,
        },
        "request_state": {
            "user_present": user is not None,
            "user": user if user else None,
        },
        "token_info": token_info,
        "headers": {
            "host": request.headers.get("host"),
            "user_agent": request.headers.get("user-agent"),
            "referer": request.headers.get("referer"),
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
    """
    from .auth import verify_jwt_token
    from .config import settings
    from .database import get_user_session, delete_user_session
    import json
    
    if not session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="session_id parameter required")
    
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
        token_data = await exchange_github_code(code)
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
        user = {"user_id": actual_user_id, "email": actual_email, "github_id": actual_github_id}
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
        user = {"user_id": actual_user_id, "email": actual_email, "google_id": actual_google_id}
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
                # Still try to return response even if cookie setting failed
                response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
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
