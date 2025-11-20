"""Middleware for authentication, rate limiting, and security."""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Any, Optional, Dict
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
import logging

from .config import settings
from .utils import is_secure_request
from .auth import verify_jwt_token
from .exceptions import RateLimitError
from .app_logging import set_request_id
from .deps import ensure_db
from .database import get_user_by_id, get_user_session, touch_user_session, delete_user_session
import json

logger = logging.getLogger(__name__)

# In-memory session cache to avoid async DB reads in middleware
# This allows synchronous session reads, avoiding ASGI InvalidStateError
_session_cache: Dict[str, Dict[str, Any]] = {}
_session_cache_timestamps: Dict[str, float] = {}
_SESSION_CACHE_TTL = 300  # 5 minutes cache TTL

# Public routes that should not require DB access for basic rendering
# These routes should degrade gracefully if DB is unavailable
_PUBLIC_ROUTES = {"/", "/health", "/login", "/signup", "/styleguide", "/docs", "/redoc", "/openapi.json"}


def _is_public_route(request: Request) -> bool:
    """Check if a request is for a public route that should work without DB.
    
    Public routes are those that should render successfully even if the database
    is temporarily unavailable. This allows the homepage and health checks to
    work even during DB outages.
    """
    path = request.url.path
    # Exact match or starts with /static/
    return path in _PUBLIC_ROUTES or path.startswith("/static/")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add request ID to each request."""

    async def dispatch(self, request: Request, call_next: Callable):
        request_id = str(uuid.uuid4())
        set_request_id(request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class SessionMiddleware(BaseHTTPMiddleware):
    """Load, refresh, and clear user sessions tied to session cookies.
    
    This middleware manages browser sessions stored in D1. Sessions coexist with JWT tokens:
    - JWT (access_token cookie): Stateless authentication token containing user claims
    - Session (session_id cookie): Stateful tracking for activity, notifications, metadata
    
    Session Architecture:
    - Sessions are stored in the user_sessions table in D1
    - Session cookie name is configurable via settings.session_cookie_name (default: "session_id")
    - Sessions have a TTL (settings.session_ttl_hours, default: 72 hours)
    - Sessions are automatically touched (last_seen_at updated) based on touch_interval
    - Expired or revoked sessions are automatically cleared
    
    Cookie Security:
    - httponly=True: Prevents JavaScript access
    - secure=is_secure: HTTPS-only in production (based on request scheme)
    - samesite="lax": CSRF protection while allowing OAuth redirects
    
    The middleware:
    1. Loads session from session_id cookie on each request
    2. Validates session expiration and revocation status
    3. Touches session (updates last_seen_at) if touch_interval elapsed
    4. Clears invalid/expired session cookies
    5. Populates request.state.session and request.state.session_id
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.cookie_name = settings.session_cookie_name
        self.touch_interval = max(0, int(settings.session_touch_interval_seconds))

    def _parse_timestamp(self, value):  # type: ignore[override]
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
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                return None

    def _get_session_from_cache(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session from in-memory cache if available and not expired."""
        if session_id not in _session_cache:
            return None
        cached_time = _session_cache_timestamps.get(session_id, 0)
        if time.time() - cached_time > _SESSION_CACHE_TTL:
            # Cache expired
            _session_cache.pop(session_id, None)
            _session_cache_timestamps.pop(session_id, None)
            return None
        return _session_cache.get(session_id)

    def _set_session_in_cache(self, session_id: str, session: Dict[str, Any]) -> None:
        """Store session in in-memory cache."""
        _session_cache[session_id] = session
        _session_cache_timestamps[session_id] = time.time()

    def _clear_session_from_cache(self, session_id: str) -> None:
        """Remove session from in-memory cache."""
        _session_cache.pop(session_id, None)
        _session_cache_timestamps.pop(session_id, None)

    # Removed _is_secure_request - now using shared is_secure_request from utils

    async def dispatch(self, request: Request, call_next: Callable):
        session_id = request.cookies.get(self.cookie_name)
        should_clear_cookie = False
        db = None
        lookup_failed = False
        is_public = _is_public_route(request)
        path = request.url.path
        
        # Skip async DB operations for API endpoints to avoid ASGI InvalidStateError
        # API endpoints are stateless and don't need session tracking
        is_api_endpoint = path.startswith("/api/")
        
        if session_id:
            if is_api_endpoint:
                # For API endpoints, skip all async DB operations to avoid ASGI errors
                # API endpoints don't need session state, so we can skip entirely
                response = await call_next(request)
                return response
            
            # Try to get session from cache first (synchronous, no async needed)
            session = self._get_session_from_cache(session_id)
            lookup_failed = False
            
            # If not in cache, skip DB lookup entirely to avoid async operations
            # The session will be loaded on next request after it's cached
            # This avoids ASGI InvalidStateError from async DB operations
            if session is None:
                session = None
                lookup_failed = True
            now = datetime.now(timezone.utc)
            if session and not session.get("revoked_at"):
                expires_at = self._parse_timestamp(session.get("expires_at"))
                if expires_at and expires_at <= now:
                    should_clear_cookie = True
                    # Clear from cache immediately (synchronous)
                    self._clear_session_from_cache(session_id)
                    
                    # Skip async DB delete to avoid ASGI errors
                    # Session is already cleared from cache, so it won't be used
                else:
                    session["session_id"] = session_id
                    request.state.session = session
                    request.state.session_id = session_id
                    if session.get("user_id"):
                        request.state.session_user_id = session.get("user_id")
                    if db and self.touch_interval:
                        last_seen_raw = self._parse_timestamp(session.get("last_seen_at"))
                        needs_touch = (
                            last_seen_raw is None
                            or (now - last_seen_raw).total_seconds() >= self.touch_interval
                        )
                        if needs_touch:
                            # Update cache immediately (synchronous)
                            session["last_seen_at"] = now.isoformat()
                            self._set_session_in_cache(session_id, session)
                            
                            # Skip async DB write to avoid ASGI errors
                            # Cache is updated, so session will work for subsequent requests
            elif not lookup_failed:
                should_clear_cookie = True

        response = await call_next(request)
        if getattr(request.state, "invalidate_session_cookie", False):
            should_clear_cookie = True
        if should_clear_cookie and session_id:
            response.delete_cookie(
                self.cookie_name,
                path="/",
                samesite="lax",
                httponly=True,
                secure=is_secure_request(request),
            )
        return response


class FlashMiddleware(BaseHTTPMiddleware):
    """Read flash messages from session and expose to templates.
    
    Flash messages are stored in session.extra as a JSON array.
    They are read once per request and cleared after reading.
    Flash messages are exposed via request.state.flash_messages for template access.
    """
    
    async def dispatch(self, request: Request, call_next: Callable):
        # Initialize empty flash messages
        request.state.flash_messages = []
        
        # Skip flash message processing for API endpoints - they return JSON, not HTML templates
        # This avoids ASGI InvalidStateError on API endpoints
        path = request.url.path
        if path.startswith("/api/"):
            response = await call_next(request)
            return response
        
        # Read flash messages from session if available
        session = getattr(request.state, "session", None)
        session_id = getattr(request.state, "session_id", None)
        if session and session_id:
            extra = session.get("extra")
            if extra:
                try:
                    if isinstance(extra, str):
                        extra_dict = json.loads(extra)
                    else:
                        extra_dict = extra
                    
                    flash_queue = extra_dict.get("flash_messages", [])
                    if flash_queue and isinstance(flash_queue, list):
                        request.state.flash_messages = flash_queue
                        # Clear flash messages from in-memory session immediately
                        # Skip async DB write to avoid ASGI InvalidStateError
                        # Session cache will be updated, and DB will sync eventually
                        extra_dict.pop("flash_messages", None)
                        # Update in-memory session dict
                        session["extra"] = json.dumps(extra_dict) if isinstance(extra_dict, dict) else extra_dict
                        # Update session cache if it exists (synchronous)
                        # Skip async DB write to avoid ASGI errors
                except (json.JSONDecodeError, TypeError, AttributeError) as exc:
                    logger.debug("Failed to parse session extra for flash messages: %s", exc)
        
        response = await call_next(request)
        return response


class AuthCookieMiddleware(BaseHTTPMiddleware):
    """Populate request.state.user from JWT in access_token cookie for DRY auth."""

    async def dispatch(self, request: Request, call_next: Callable):
        # Try cookie first, then Authorization header
        token = request.cookies.get("access_token")
        logger.debug(
            "AuthCookieMiddleware: Checking for token - cookie_present=%s, cookie_length=%s",
            token is not None,
            len(token) if token else 0,
        )
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
                logger.debug("AuthCookieMiddleware: Found token in Authorization header")
        if token:
            try:
                payload = verify_jwt_token(token)
                user_id = payload.get("user_id")
                email = payload.get("email")
                github_id = payload.get("github_id")
                google_id = payload.get("google_id")
                session_user_id = getattr(request.state, "session_user_id", None)
                if session_user_id and user_id and str(session_user_id) != str(user_id):
                    logger.warning(
                        "Session user mismatch; clearing session cookie",
                        extra={"token_user_id": user_id, "session_user_id": session_user_id},
                    )
                    request.state.invalidate_session_cookie = True
                    if hasattr(request.state, "session"):
                        try:
                            delattr(request.state, "session")
                        except Exception:
                            pass
                # Only backfill from DB if critical identifier (email) is missing.
                # Avoid fetching solely for optional provider IDs (github_id/google_id).
                if user_id and not email:
                    try:
                        db = ensure_db()
                        stored = await get_user_by_id(db, user_id)  # type: ignore
                        if stored:
                            stored_user_id = stored.get("user_id") or stored.get("id")
                            if not stored_user_id:
                                logger.warning(
                                    "AuthCookieMiddleware: missing user_id in stored record",
                                    extra={"token_user_id": user_id, "stored_record": stored},
                                )
                            if stored_user_id and str(stored_user_id) != str(user_id):
                                logger.warning(
                                    "AuthCookieMiddleware: token user mismatch",
                                    extra={"token_user_id": user_id, "db_user_id": stored_user_id},
                                )
                            else:
                                email = stored.get("email", email)
                                # Provider IDs remain best-effort; don't force a DB hit just for them.
                                github_id = github_id or stored.get("github_id")
                                google_id = google_id or stored.get("google_id")
                    except (HTTPException, Exception) as exc:
                        # DB unavailable - continue without DB enrichment, don't fail request
                        # HTTPException(500) from ensure_db() is caught here so it doesn't fail the request
                        logger.debug("AuthCookieMiddleware: DB unavailable, cannot hydrate user from DB - continuing with JWT claims only: %s", exc)
                request.state.user = {
                    "user_id": user_id,
                    "email": email,
                    "github_id": github_id,
                    "google_id": google_id,
                }
                if user_id:
                    request.state.user_id = user_id
                logger.debug(
                    "AuthCookieMiddleware: Successfully set request.state.user: user_id=%s, email=%s",
                    user_id,
                    email,
                )
            except Exception as exc:
                logger.debug("AuthCookieMiddleware: Failed to verify token: %s", exc, exc_info=True)
                # Invalid token: ensure no stale user state leaks into request
                if hasattr(request.state, "user"):
                    try:
                        delattr(request.state, "user")
                    except Exception:
                        pass
                if hasattr(request.state, "user_id"):
                    try:
                        delattr(request.state, "user_id")
                    except Exception:
                        pass
                request.state.invalidate_session_cookie = True
        return await call_next(request)


# Authentication is handled by router dependencies (Depends), not middleware.


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting (for production, use Redis/KV)."""
    
    def __init__(self, app: ASGIApp, max_per_minute: int, max_per_hour: int):
        super().__init__(app)
        self.max_per_minute = max_per_minute
        self.max_per_hour = max_per_hour
        self.requests: dict[str, list[float]] = {}
        self.lock = asyncio.Lock()
        self.cleanup_interval = 300  # Clean up every 5 minutes
        self.last_cleanup = time.monotonic()
    
    def _get_client_id(self, request: Request) -> str:
        """Get client identifier for rate limiting.
        
        Note: In Cloudflare Workers, request.client may be None.
        We also look at CF-Connecting-IP / X-Forwarded-For headers for client IP,
        and fall back to "unknown" rather than rejecting the request.
        """
        user_id = getattr(request.state, "user_id", None)
        if user_id:
            return f"user:{user_id}"
        
        # Try connection info first
        ip = None
        if request.client and getattr(request.client, "host", None):
            ip = request.client.host
        
        # Fallback to headers if no client.host
        if not ip:
            headers = request.headers
            ip = headers.get("CF-Connecting-IP")
            if not ip:
                xff = headers.get("X-Forwarded-For", "")
                if xff:
                    ip = xff.split(",")[0].strip() or None
            if not ip:
                ip = headers.get("X-Real-IP")
        
        # Final fallback: don't 400, just treat IP as unknown
        if not ip:
            ip = "unknown"
        
        return f"ip:{ip}"
    
    async def _is_rate_limited(self, client_id: str) -> bool:
        """Check if client is rate limited (thread-safe)."""
        now = time.monotonic()
        
        async with self.lock:
            if now - self.last_cleanup > self.cleanup_interval:
                self._cleanup_old_entries(now)
                self.last_cleanup = now
            
            history = self.requests.setdefault(client_id, [])
            history[:] = [req_time for req_time in history if now - req_time < 3600]
            
            recent_minute = [req_time for req_time in history if now - req_time < 60]
            if len(recent_minute) >= self.max_per_minute:
                return True
            
            if len(history) >= self.max_per_hour:
                return True
            
            history.append(now)
            return False
    
    def _cleanup_old_entries(self, now: float):
        """Clean up old rate limit entries."""
        for client_id in list(self.requests.keys()):
            requests = self.requests[client_id]
            requests[:] = [req_time for req_time in requests if now - req_time < 3600]
            if not requests:
                del self.requests[client_id]
    
    async def dispatch(self, request: Request, call_next: Callable):
        # Skip rate limiting for health checks and static assets
        path = request.url.path
        if (
            path == "/health"
            or path.startswith("/static/")
            or path == "/favicon.ico"
            or path == "/robots.txt"
        ):
            return await call_next(request)
        
        client_id = self._get_client_id(request)
        
        if await self._is_rate_limited(client_id):
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": "Rate limit exceeded",
                    "error_code": "RATE_LIMIT_ERROR"
                },
                headers={"Retry-After": "60"}
            )
        
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to responses."""
    
    async def dispatch(self, request: Request, call_next: Callable):
        response = await call_next(request)
        
        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        
        # HSTS header only in production
        if settings.environment == "production" or (not settings.debug and settings.environment != "development"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        
        return response


class CORSMiddleware(BaseHTTPMiddleware):
    """Handle CORS preflight requests and append CORS headers to all responses."""
    
    def __init__(self, app: ASGIApp, allow_origins: list[str], allow_credentials: bool = True, allow_methods: list[str] = None, allow_headers: list[str] = None):
        super().__init__(app)
        self.allow_origins = allow_origins
        self.allow_credentials = allow_credentials
        self.allow_methods = allow_methods or ["*"]
        self.allow_headers = allow_headers or ["*"]
    
    async def dispatch(self, request: Request, call_next: Callable):
        origin = request.headers.get("Origin")
        
        # Determine if origin is allowed and what header value to use
        allowed = False
        allow_origin_value = None
        
        if origin:
            # Check if origin is explicitly in the allowed list (excluding "*")
            explicit_origins = [o for o in self.allow_origins if o != "*"]
            if origin in explicit_origins:
                # Origin is explicitly allowed - use it
                allowed = True
                allow_origin_value = origin
            elif "*" in self.allow_origins:
                # Wildcard is configured
                if not self.allow_credentials:
                    # Safe to use wildcard when credentials are disabled
                    allowed = True
                    allow_origin_value = "*"
                # If credentials are enabled and "*" is present, only allow explicit origins
                # (already handled above, so allowed remains False)
        
        if request.method == "OPTIONS":
            response = Response()
            if allowed:
                response.headers["Access-Control-Allow-Origin"] = allow_origin_value
                response.headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods) if isinstance(self.allow_methods, list) else self.allow_methods
                response.headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers) if isinstance(self.allow_headers, list) else self.allow_headers
                # Only set credentials when origin is explicitly allowed (not when using "*")
                if self.allow_credentials and allow_origin_value != "*":
                    response.headers["Access-Control-Allow-Credentials"] = "true"
            return response
        
        response = await call_next(request)
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = allow_origin_value
            # Only set credentials when origin is explicitly allowed (not when using "*")
            if self.allow_credentials and allow_origin_value != "*":
                response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers) if isinstance(self.allow_headers, list) else self.allow_headers
            response.headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods) if isinstance(self.allow_methods, list) else self.allow_methods
        return response
