"""
Middleware for authentication, rate limiting, and security.
"""

import time
import uuid
from typing import Callable, Optional
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
import logging
import threading

from .config import settings
from .auth import verify_jwt_token, authenticate_api_key
from .database import Database, get_user_by_id
from .exceptions import AuthenticationError, RateLimitError
from .app_logging import set_request_id, get_request_id

logger = logging.getLogger(__name__)

# Shared database instance (lazily initialized)
_db_instance: Optional[Database] = None
# Lock to make initialization thread-safe
_db_init_lock = threading.Lock()


def get_database() -> Optional[Database]:
    """Get or create a shared Database instance using double-checked locking."""
    global _db_instance
    if _db_instance is None:
        with _db_init_lock:
            if _db_instance is None:
                # Try to get from deps first (set by main.lifespan)
                try:
                    import api.deps as deps_module
                    deps_db = getattr(deps_module, '_db_instance', None)
                    if deps_db:
                        _db_instance = deps_db
                        logger.info("Middleware: Using database from deps")
                except Exception as e:
                    logger.debug(f"Middleware: Could not get database from deps: {e}")
                
                if _db_instance is None:
                    if settings.d1_database:
                        # Cloudflare D1 database
                        _db_instance = Database(db=settings.d1_database)
                        logger.info("Middleware: Created D1 database instance")
                    else:
                        # Local SQLite fallback
                        _db_instance = Database()
                        logger.info("Middleware: Created SQLite database instance")
    return _db_instance


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add request ID to each request."""
    
    async def dispatch(self, request: Request, call_next: Callable):
        request_id = str(uuid.uuid4())
        set_request_id(request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Handle authentication for protected routes."""
    
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        # Database will be accessed via settings/dependency injection
    
    async def dispatch(self, request: Request, call_next: Callable):
        # Skip auth for public endpoints
        public_paths = [
            "/",
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/login",
            "/auth/github/start",
            "/auth/github/callback",
            "/auth/logout",
        ]
        
        # Check if path is exactly in public_paths or starts with a public path (but not just "/")
        is_public = False
        for path in public_paths:
            if path == "/":
                if request.url.path == "/":
                    is_public = True
                    break
            elif request.url.path.startswith(path):
                is_public = True
                break
        
        if is_public:
            return await call_next(request)
        
        user = None
        
        # Get authorization header
        auth_header = request.headers.get("Authorization")
        
        # Try API key first (if present in header)
        if auth_header and auth_header.startswith("ApiKey "):
            api_key = auth_header.replace("ApiKey ", "")
            # Get shared database instance (reused across requests)
            db = get_database()
            if db:
                user = await authenticate_api_key(db, api_key)
            else:
                # For local testing, reject API keys without database
                user = None
        
        # If no user from API key, try JWT token
        if not user:
            token = None
            
            # Try to get token from Authorization header first
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.replace("Bearer ", "")
            
            # If no token in header, try to get from cookie
            if not token:
                token = request.cookies.get("access_token")
            
            # If still no token, return unauthorized
            if not token:
                if settings.debug:
                    logger.debug(f"Authentication failed - no token found for {request.url.path}")
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"error": "Authentication required", "error_code": "AUTH_ERROR"}
                )
            
            # Try JWT token
            try:
                payload = verify_jwt_token(token)
                if payload:
                    user_id = payload.get("user_id")
                    # Get shared database instance (reused across requests)
                    db = get_database()
                    if db:
                        user = await get_user_by_id(db, user_id)
                        if not user:
                            logger.warning(f"Token valid but user not found in database for user_id: {user_id}")
                    else:
                        # Only allow mocking in non-production environments
                        if settings.debug or settings.environment != "production":
                            logger.warning("Mocking user due to missing database (development only)")
                            user = {"user_id": user_id, "github_id": None, "email": None, "created_at": "2025-01-01T00:00:00"}
                        else:
                            logger.error("Database unavailable in production environment; rejecting request")
                            return JSONResponse(
                                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                content={"error": "Authentication service unavailable", "error_code": "AUTH_ERROR"}
                            )
            except AuthenticationError as e:
                logger.debug(f"Token verification failed: {e}")
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"error": "Invalid or expired token", "error_code": "AUTH_ERROR"}
                )
            except ValueError as e:
                # Malformed token or payload
                logger.debug(f"Token validation error: {e}")
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"error": "Invalid token format", "error_code": "AUTH_ERROR"}
                )
            except Exception as e:
                # Unexpected errors (database, network, etc.) should be 500, not 401
                logger.error(f"Unexpected error during token verification: {e}", exc_info=True)
                return JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content={"error": "Authentication service error", "error_code": "AUTH_ERROR"}
                )
        
        if not user:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "Invalid credentials", "error_code": "AUTH_ERROR"}
            )
        
        # Validate user structure before attaching
        if not isinstance(user, dict) or "user_id" not in user or not user.get("user_id"):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "Invalid user credentials", "error_code": "AUTH_ERROR"}
            )
        # Attach user to request state
        request.state.user = user
        request.state.user_id = user.get("user_id")
        
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting (for production, use Redis/KV)."""
    
    def __init__(self, app: ASGIApp):
        super().__init__(app)
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
            if len(recent_minute) >= settings.rate_limit_per_minute:
                return True
            
            # Check per-hour limit
            if len(requests) >= settings.rate_limit_per_hour:
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
        if request.url.path in ["/health", "/"]:
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
    
    async def dispatch(self, request: Request, call_next: Callable):
        origin = request.headers.get("Origin")
        allowed = origin and (origin in settings.cors_origins or "*" in settings.cors_origins)
        
        if request.method == "OPTIONS":
            response = Response()
            if allowed:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
                response.headers["Access-Control-Allow-Credentials"] = "true"
            return response
        
        response = await call_next(request)
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return response

