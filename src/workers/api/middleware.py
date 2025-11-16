"""
Middleware for authentication, rate limiting, and security.
"""

import time
import uuid
from typing import Callable
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
import logging
import threading

from .config import settings
from .auth import verify_jwt_token
from .exceptions import RateLimitError
from .app_logging import set_request_id
from .deps import ensure_db
from .database import get_user_by_id

logger = logging.getLogger(__name__)

_db_init_lock = threading.Lock()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add request ID to each request."""
    
    async def dispatch(self, request: Request, call_next: Callable):
        request_id = str(uuid.uuid4())
        set_request_id(request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class AuthCookieMiddleware(BaseHTTPMiddleware):
    """Populate request.state.user from JWT in access_token cookie for DRY auth."""

    async def dispatch(self, request: Request, call_next: Callable):
        token = request.cookies.get("access_token")
        if token:
            try:
                payload = verify_jwt_token(token)
                user_id = payload.get("user_id")
                email = payload.get("email")
                github_id = payload.get("github_id")
                google_id = payload.get("google_id")
                # Only backfill from DB if critical identifier (email) is missing.
                # Avoid fetching solely for optional provider IDs (github_id/google_id).
                if user_id and not email:
                    try:
                        db = ensure_db()
                        stored = await get_user_by_id(db, user_id)  # type: ignore
                        if stored:
                            email = stored.get("email", email)
                            # Provider IDs remain best-effort; don't force a DB hit just for them.
                            github_id = github_id or stored.get("github_id")
                            google_id = google_id or stored.get("google_id")
                    except Exception as exc:
                        logger.debug("AuthCookieMiddleware: failed to fetch user profile: %s", exc)
                request.state.user = {
                    "user_id": user_id,
                    "email": email,
                    "github_id": github_id,
                    "google_id": google_id,
                }
                if user_id:
                    request.state.user_id = user_id
            except Exception:
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
        return await call_next(request)


# Authentication is handled by router dependencies (Depends), not middleware.


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting (for production, use Redis/KV)."""
    
    def __init__(self, app: ASGIApp, max_per_minute: int, max_per_hour: int):
        super().__init__(app)
        self.max_per_minute = max_per_minute
        self.max_per_hour = max_per_hour
        self.requests: dict[str, list[float]] = {}
        self.lock = threading.Lock()
        self.cleanup_interval = 300  # Clean up every 5 minutes
        self.last_cleanup = time.time()
    
    def _get_client_id(self, request: Request) -> str:
        """Get client identifier for rate limiting."""
        user_id = getattr(request.state, "user_id", None)
        if user_id:
            return f"user:{user_id}"
        # Fall back to IP address
        return f"ip:{request.client.host if request.client else 'unknown'}"
    
    def _is_rate_limited(self, client_id: str) -> bool:
        """Check if client is rate limited (thread-safe)."""
        now = time.time()
        
        with self.lock:
            # Cleanup old entries periodically
            if now - self.last_cleanup > self.cleanup_interval:
                self._cleanup_old_entries(now)
                self.last_cleanup = now
            
            # Get request history
            if client_id not in self.requests:
                self.requests[client_id] = []
            
            requests = self.requests[client_id]
            
            # Remove requests older than 1 hour
            requests[:] = [req_time for req_time in requests if now - req_time < 3600]
            
            # Check per-minute limit
            recent_minute = [req_time for req_time in requests if now - req_time < 60]
            if len(recent_minute) >= self.max_per_minute:
                return True
            
            # Check per-hour limit
            if len(requests) >= self.max_per_hour:
                return True
            
            # Add current request
            requests.append(now)
            return False
    
    def _cleanup_old_entries(self, now: float):
        """Clean up old rate limit entries."""
        for client_id in list(self.requests.keys()):
            requests = self.requests[client_id]
            requests[:] = [req_time for req_time in requests if now - req_time < 3600]
            if not requests:
                del self.requests[client_id]
    
    async def dispatch(self, request: Request, call_next: Callable):
        # Skip rate limiting for health checks
        if request.url.path in ["/health"]:
            return await call_next(request)
        
        client_id = self._get_client_id(request)
        if self._is_rate_limited(client_id):
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
        response.headers["X-XSS-Protection"] = "1; mode=block"
        
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
        allowed = origin and (origin in self.allow_origins or "*" in self.allow_origins)
        
        if request.method == "OPTIONS":
            response = Response()
            if allowed:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods) if isinstance(self.allow_methods, list) else self.allow_methods
                response.headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers) if isinstance(self.allow_headers, list) else self.allow_headers
                if self.allow_credentials:
                    response.headers["Access-Control-Allow-Credentials"] = "true"
            return response
        
        response = await call_next(request)
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            if self.allow_credentials:
                response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers) if isinstance(self.allow_headers, list) else self.allow_headers
            response.headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods) if isinstance(self.allow_methods, list) else self.allow_methods
        return response
