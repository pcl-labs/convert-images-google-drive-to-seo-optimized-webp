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
from .exceptions import RateLimitError
from .app_logging import set_request_id

logger = logging.getLogger(__name__)

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


# Removed SessionMiddleware, FlashMiddleware, and AuthCookieMiddleware - authentication is now handled by Better Auth


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
