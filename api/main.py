"""
Production-ready FastAPI web application for Google Drive Image Optimizer.
"""

from fastapi import FastAPI, Request, Depends, HTTPException, status, Form
from fastapi.responses import JSONResponse, RedirectResponse
from contextlib import asynccontextmanager
from typing import Optional
import uuid
import inspect
import secrets
import json
from datetime import datetime, timezone

from .config import settings
from .models import (
    OptimizeRequest,
    JobStatus,
    JobProgress,
    JobListResponse,
    UserResponse,
    APIKeyResponse,
    HealthResponse,
    StatsResponse,
    JobStatusEnum
)
from .database import (
    Database,
    create_job,
    get_job,
    list_jobs,
    get_job_stats,
    get_user_count,
    get_user_by_id,
    update_job_status
)
from .auth import (
    authenticate_github,
    create_user_api_key,
)
from . import auth as auth
from .cloudflare_queue import QueueProducer
from .constants import COOKIE_OAUTH_STATE, COOKIE_GOOGLE_OAUTH_STATE
from .google_oauth import get_google_oauth_url, exchange_google_code, build_drive_service_for_user
from .database import get_google_tokens
from .exceptions import (
    APIException,
    JobNotFoundError,
)
from .app_logging import setup_logging, get_logger, get_request_id
from .middleware import (
    RequestIDMiddleware,
    AuthenticationMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    CORSMiddleware
)
from core.drive_utils import extract_folder_id_from_input
from .deps import (
    ensure_db,
    ensure_services,
    get_current_user,
    parse_job_progress,
    set_db_instance,
    set_queue_producer,
)

# Set up logging
logger = setup_logging(level="INFO" if not settings.debug else "DEBUG", use_json=True)
app_logger = get_logger(__name__)


# Database and queue instances (will be bound at runtime)
db_instance: Optional[Database] = None
queue_producer: Optional[QueueProducer] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global db_instance, queue_producer
    
    # Startup
    app_logger.info("Starting application")
    
    # Initialize database
    db_instance = Database(db=settings.d1_database)
    app_logger.info("Database initialized")
    # Expose to shared deps
    set_db_instance(db_instance)
    
    # Initialize queue producer
    queue_producer = QueueProducer(queue=settings.queue, dlq=settings.dlq)
    app_logger.info("Queue producer initialized")
    # Expose to shared deps
    set_queue_producer(queue_producer)
    
    # Add authentication middleware after db is initialized
    # Note: This is a workaround - in production, middleware should be added before app creation
    # For now, we'll handle auth in dependencies instead
    
    yield
    
    # Shutdown
    app_logger.info("Shutting down application")
    
    # Cleanup database connection
    if db_instance is not None:
        try:
            # Check if the underlying db object has a close method
            if hasattr(db_instance, 'db') and db_instance.db is not None:
                db_obj = db_instance.db
                # Check for common close/disconnect method names
                for method_name in ['close', 'disconnect', 'cleanup']:
                    if hasattr(db_obj, method_name):
                        method = getattr(db_obj, method_name)
                        if inspect.iscoroutinefunction(method):
                            await method()
                        else:
                            method()
                        app_logger.info(f"Database {method_name} called successfully")
                        break
            app_logger.info("Database connection closed")
        except Exception as e:
            app_logger.error(f"Error closing database connection: {e}", exc_info=True)
        finally:
            db_instance = None
    
    # Cleanup queue producer
    if queue_producer is not None:
        try:
            # Check if the underlying queue object has a close method
            if hasattr(queue_producer, 'queue') and queue_producer.queue is not None:
                queue_obj = queue_producer.queue
                # Check for common close/stop method names
                for method_name in ['close', 'stop', 'cleanup', 'shutdown']:
                    if hasattr(queue_obj, method_name):
                        method = getattr(queue_obj, method_name)
                        if inspect.iscoroutinefunction(method):
                            await method()
                        else:
                            method()
                        app_logger.info(f"Queue {method_name} called successfully")
                        break
            app_logger.info("Queue producer closed")
        except Exception as e:
            app_logger.error(f"Error closing queue producer: {e}", exc_info=True)
        finally:
            queue_producer = None


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    description="Production-ready API for optimizing images from Google Drive to WebP format",
    version=settings.app_version,
    lifespan=lifespan
)

# Mount HTML web routes
from .web import router as web_router
app.include_router(web_router)

# Add middleware (order matters!)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CORSMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(AuthenticationMiddleware)
app.add_middleware(RateLimitMiddleware)


# Global exception handler
@app.exception_handler(APIException)
async def api_exception_handler(request: Request, exc: APIException):
    """Handle custom API exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "error_code": exc.error_code,
            "request_id": get_request_id()
        }
    )


# get_current_user is provided by deps


# Authentication endpoints
# ========================

# GitHub OAuth (Public - no auth required)
@app.get("/auth/github/start", tags=["Authentication"])
async def github_auth_start(request: Request):
    """Initiate GitHub OAuth flow."""
    try:
        # Build redirect URI: use BASE_URL if set, otherwise use request URL
        if settings.base_url:
            redirect_uri = f"{settings.base_url.rstrip('/')}/auth/github/callback"
        else:
            redirect_uri = str(request.url.replace(path="/auth/github/callback", query=""))
        auth_url, state = auth.get_github_oauth_url(redirect_uri)
        
        # Determine if we're behind HTTPS (production)
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        
        # Create redirect response
        response = RedirectResponse(url=auth_url)
        
        # Store state in secure cookie for CSRF protection
        # State expires in 10 minutes (enough time for OAuth flow)
        response.set_cookie(
            key=COOKIE_OAUTH_STATE,
            value=state,
            httponly=True,
            secure=is_secure,
            samesite="lax",
            max_age=600,  # 10 minutes
            path="/"
        )
        
        return response
    except Exception as e:
        app_logger.error(f"GitHub auth initiation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub OAuth not configured"
        )


@app.post("/auth/github/start", tags=["Authentication"])
async def github_auth_start_post(request: Request, csrf_token: str = Form(...)):
    """Initiate GitHub OAuth flow via POST with CSRF protection."""
    # Validate CSRF token from form against cookie
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or not secrets.compare_digest(cookie_token, csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid CSRF token",
        )
    try:
        # Build redirect URI: use BASE_URL if set, otherwise use request URL
        if settings.base_url:
            redirect_uri = f"{settings.base_url.rstrip('/')}/auth/github/callback"
        else:
            redirect_uri = str(request.url.replace(path="/auth/github/callback", query=""))
        auth_url, state = auth.get_github_oauth_url(redirect_uri)

        # Determine if we're behind HTTPS (production)
        is_secure = settings.environment == "production" or request.url.scheme == "https"

        # Create redirect response
        response = RedirectResponse(url=auth_url)

        # Store state in secure cookie for CSRF protection (10 minutes)
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
    except Exception as e:
        app_logger.error(f"GitHub auth initiation (POST) failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub OAuth not configured",
        )

@app.get("/auth/logout", tags=["Authentication"])
async def logout(request: Request):
    """Sign out the current user by clearing the access token cookie."""
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    # Clear the access token cookie
    response.delete_cookie(
        key="access_token",
        path="/",
        samesite="lax"
    )
    
    # Also clear CSRF token for good measure
    response.delete_cookie(
        key="csrf_token",
        path="/",
        samesite="lax"
    )
    
    return response


@app.get("/auth/github/callback", tags=["Authentication"])
async def github_callback(code: str, state: str, request: Request):
    """Handle GitHub OAuth callback."""
    db = ensure_db()
    
    # Verify CSRF state token
    stored_state = request.cookies.get(COOKIE_OAUTH_STATE)
    if not stored_state or not secrets.compare_digest(stored_state, state):
        app_logger.warning("OAuth state verification failed - possible CSRF attack")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid state parameter - possible CSRF attack"
        )
    
    try:
        jwt_token, user = await authenticate_github(db, code)
        
        # Prepare user response
        user_response = {
            "user_id": user["user_id"],
            "email": user.get("email"),
            "github_id": user.get("github_id")
        }
        
        # Determine if we're behind HTTPS (production)
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        
        # Use secure cookies in production, body in dev (if configured)
        if settings.jwt_use_cookies:
            # Calculate max_age from JWT expiration
            max_age_seconds = settings.jwt_expiration_hours * 3600
            
            # Redirect to dashboard after successful login
            response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
            
            # Set secure HTTP-only cookie
            # Don't set domain at all - let browser use default (works for both localhost and 127.0.0.1)
            # Setting domain=None explicitly can cause issues, so we omit it entirely
            response.set_cookie(
                key="access_token",
                value=jwt_token,
                httponly=True,
                secure=is_secure,
                samesite="lax",
                max_age=max_age_seconds,
                path="/"
                # No domain parameter - browser will use default behavior
            )
            
            # Clear the OAuth state cookie after successful verification
            response.delete_cookie(
                key=COOKIE_OAUTH_STATE,
                path="/",
                samesite="lax"
            )
            
            return response
        else:
            # Fallback to body for local/dev environments
            # Create response to clear state cookie
            response = JSONResponse(content={
                "access_token": jwt_token,
                "token_type": "bearer",
                "user": user_response
            })
            
            # Clear the OAuth state cookie after successful verification
            response.delete_cookie(
                key=COOKIE_OAUTH_STATE,
                path="/",
                samesite="lax"
            )
            
            return response
    except Exception as e:
        app_logger.error(f"GitHub callback failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(e)}"
        )


# GitHub OAuth (Protected - requires auth)
@app.get("/auth/github/status", tags=["Authentication"])
async def github_link_status(user: dict = Depends(get_current_user)):
    """Return whether the current user has linked GitHub (always true if authenticated)."""
    # If we got here, JWT is valid so GitHub session is established
    return {
        "linked": True,
        "github_id": user.get("github_id"),
        "email": user.get("email")
    }


# Google OAuth (Protected - requires auth)
@app.get("/auth/google/start", tags=["Authentication"])
async def google_auth_start(request: Request, user: dict = Depends(get_current_user)):
    """Initiate Google OAuth flow for the authenticated user."""
    try:
        # Build redirect URI: use BASE_URL if set, otherwise use request URL
        if settings.base_url:
            redirect_uri = f"{settings.base_url.rstrip('/')}/auth/google/callback"
        else:
            redirect_uri = str(request.url.replace(path="/auth/google/callback", query=""))
        state = secrets.token_urlsafe(16)
        auth_url = get_google_oauth_url(state, redirect_uri)

        is_secure = settings.environment == "production" or request.url.scheme == "https"
        response = RedirectResponse(url=auth_url)
        response.set_cookie(
            key=COOKIE_GOOGLE_OAUTH_STATE,
            value=state,
            httponly=True,
            secure=is_secure,
            samesite="lax",
            max_age=600,
            path="/",
        )
        # Store redirect_uri in cookie for callback verification
        response.set_cookie(
            key="google_redirect_uri",
            value=redirect_uri,
            httponly=True,
            secure=is_secure,
            samesite="lax",
            max_age=600,
            path="/",
        )
        return response
    except Exception as e:
        app_logger.error(f"Google auth initiation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth not configured"
        )


@app.get("/auth/google/callback", tags=["Authentication"])
async def google_auth_callback(code: str, state: str, request: Request, user: dict = Depends(get_current_user)):
    """Handle Google OAuth callback and store tokens for the user."""
    db = ensure_db()

    stored_state = request.cookies.get(COOKIE_GOOGLE_OAUTH_STATE)
    if not stored_state or not secrets.compare_digest(stored_state, state):
        app_logger.warning("Google OAuth state verification failed - possible CSRF attack")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid state parameter - possible CSRF attack"
        )

    # Get redirect URI from cookie (set during /start)
    redirect_uri = request.cookies.get("google_redirect_uri")
    if not redirect_uri:
        # Fallback: build from request URL
        redirect_uri = str(request.url.replace(query=""))

    try:
        await exchange_google_code(db, user["user_id"], code, redirect_uri)
        # Redirect to dashboard after successful linking
        response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax")
        response.delete_cookie(key="google_redirect_uri", path="/", samesite="lax")
        return response
    except Exception as e:
        app_logger.error(f"Google callback failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Google authentication failed: {str(e)}"
        )


@app.get("/auth/google/status", tags=["Authentication"])
async def google_link_status(user: dict = Depends(get_current_user)):
    """Return whether the current user has linked Google, with basic token info."""
    db = ensure_db()
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    if not tokens:
        return {"linked": False}
    return {
        "linked": True,
        "expiry": tokens.get("expiry"),
        "scopes": tokens.get("scopes"),
    }


# Provider status (Protected - requires auth)
@app.get("/auth/providers/status", tags=["Authentication"])
async def providers_status(user: dict = Depends(get_current_user)):
    """Unified provider status for the authenticated user."""
    db = ensure_db()
    # If we got here, JWT is valid so GitHub session is established
    github_linked = True
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    google_linked = bool(tokens)
    return {
        "github_linked": github_linked,
        "google_linked": google_linked,
        "google_expiry": tokens.get("expiry") if tokens else None,
        "google_scopes": tokens.get("scopes") if tokens else None,
    }


# User and session management (Protected - requires auth)
@app.get("/auth/me", response_model=UserResponse, tags=["Authentication"])
async def get_current_user_info(user: dict = Depends(get_current_user)):
    """Get current authenticated user information."""
    return UserResponse(
        user_id=user["user_id"],
        github_id=user.get("github_id"),
        email=user.get("email"),
        created_at=datetime.fromisoformat(user["created_at"]) if user.get("created_at") else datetime.now(timezone.utc)
    )


# API key management (Protected - requires auth)
@app.post("/auth/keys", response_model=APIKeyResponse, tags=["Authentication"])
async def create_api_key_endpoint(user: dict = Depends(get_current_user)):
    """Generate a new API key for the authenticated user."""
    db = ensure_db()
    
    try:
        api_key = await create_user_api_key(db, user["user_id"])
        return APIKeyResponse(
            api_key=api_key,
            created_at=datetime.now(timezone.utc)
        )
    except Exception as e:
        app_logger.error(f"API key creation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create API key"
        )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions."""
    app_logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "error_code": "INTERNAL_ERROR",
            "request_id": get_request_id()
        }
    )

# Utility functions
def parse_job_progress_model(progress_str: str) -> JobProgress:
    data = parse_job_progress(progress_str) or {}
    return JobProgress(**data)

# ensure_db and ensure_services provided by deps


# Public endpoints
@app.get("/", tags=["Public"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "endpoints": {
            "auth": "/auth/github/start",
            "optimize": "/api/v1/optimize",
            "jobs": "/api/v1/jobs",
            "health": "/health",
            "docs": "/docs"
        }
    }


## Debug endpoints removed before commit


@app.get("/health", response_model=HealthResponse, tags=["Public"])
async def health():
    """Health check endpoint."""
    health_data = {
        "status": "healthy",
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc)
    }
    
    # Check database
    if db_instance and db_instance.db:
        try:
            await db_instance.execute("SELECT 1")
            health_data["database"] = "connected"
        except Exception as e:
            app_logger.error(f"Database health check failed: {e}")
            health_data["database"] = "disconnected"
    else:
        health_data["database"] = "not_configured"
    
    # Check queue
    if queue_producer and queue_producer.queue:
        health_data["queue"] = "connected"
    else:
        health_data["queue"] = "not_configured"
    
    return health_data




# Job endpoints
@app.post("/api/v1/optimize", response_model=JobStatus, tags=["Jobs"])
async def optimize_images(
    request: OptimizeRequest,
    user: dict = Depends(get_current_user)
):
    """Start an image optimization job."""
    db = ensure_db()
    queue = ensure_services()[1]
    
    # Ensure user has linked Google and validate folder access using user's Drive service
    try:
        # Will raise if not linked or cannot refresh
        service = await build_drive_service_for_user(db, user["user_id"])  # type: ignore
        folder_id = extract_folder_id_from_input(request.drive_folder, service=service)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google not linked or folder not accessible: {str(e)}"
        )
    
    # Create job
    job_id = str(uuid.uuid4())
    try:
        job_data = await create_job(
            db,
            job_id,
            user["user_id"],
            request.drive_folder,
            request.extensions
        )
        
        # Send to queue
        await queue.send_job(job_id, user["user_id"], request)
        
        app_logger.info(f"Created job {job_id} for user {user['user_id']}")
        
        # Return job status
        progress = parse_job_progress_model(job_data.get("progress", "{}"))
        return JobStatus(
            job_id=job_id,
            user_id=user["user_id"],
            status=JobStatusEnum.PENDING,
            progress=progress,
            created_at=datetime.fromisoformat(job_data["created_at"]) if job_data.get("created_at") else datetime.now(timezone.utc),
            drive_folder=request.drive_folder
        )
    except Exception as e:
        app_logger.error(f"Failed to create job: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create optimization job"
        )


@app.get("/api/v1/jobs/{job_id}", response_model=JobStatus, tags=["Jobs"])
async def get_job_status(
    job_id: str,
    user: dict = Depends(get_current_user)
):
    """Get the status of an optimization job."""
    db = ensure_db()
    
    job = await get_job(db, job_id, user["user_id"]) 
    if not job:
        raise JobNotFoundError(job_id)
    
    # Parse progress data with error handling
    progress = parse_job_progress_model(job.get("progress", "{}"))
    
    return JobStatus(
        job_id=job["job_id"],
        user_id=job["user_id"],
        status=JobStatusEnum(job["status"]),
        progress=progress,
        created_at=datetime.fromisoformat(job["created_at"]),
        completed_at=datetime.fromisoformat(job["completed_at"]) if job.get("completed_at") else None,
        error=job.get("error"),
        drive_folder=job.get("drive_folder")
    )


@app.get("/api/v1/jobs", response_model=JobListResponse, tags=["Jobs"])
async def list_user_jobs(
    page: int = 1,
    page_size: int = 20,
    status_filter: Optional[JobStatusEnum] = None,
    user: dict = Depends(get_current_user)
):
    """List jobs for the authenticated user."""
    db = ensure_db()
    
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20
    
    jobs_list, total = await list_jobs(
        db,
        user["user_id"],
        page=page,
        page_size=page_size,
        status=status_filter.value if status_filter else None
    )
    
    job_statuses = []
    for job in jobs_list:
        # Parse progress data with error handling
        progress = parse_job_progress_model(job.get("progress", "{}"))
        job_statuses.append(JobStatus(
            job_id=job["job_id"],
            user_id=job["user_id"],
            status=JobStatusEnum(job["status"]),
            progress=progress,
            created_at=datetime.fromisoformat(job["created_at"]),
            completed_at=datetime.fromisoformat(job["completed_at"]) if job.get("completed_at") else None,
            error=job.get("error"),
            drive_folder=job.get("drive_folder")
        ))
    
    return JobListResponse(
        jobs=job_statuses,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total
    )


@app.delete("/api/v1/jobs/{job_id}", tags=["Jobs"])
async def cancel_job(
    job_id: str,
    user: dict = Depends(get_current_user)
):
    """Cancel a job (if it's still pending or processing)."""
    db = ensure_db()
    
    job = await get_job(db, job_id, user["user_id"])
    if not job:
        raise JobNotFoundError(job_id)
    
    current_status = JobStatusEnum(job["status"])
    if current_status in [JobStatusEnum.COMPLETED, JobStatusEnum.FAILED, JobStatusEnum.CANCELLED]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel job with status: {current_status.value}"
        )
    
    await update_job_status(db, job_id, "cancelled")
    
    app_logger.info(f"Cancelled job {job_id} for user {user['user_id']}")
    
    return {"message": "Job cancelled successfully", "job_id": job_id}


# Admin/Stats endpoints
@app.get("/api/v1/stats", response_model=StatsResponse, tags=["Admin"])
async def get_stats(user: dict = Depends(get_current_user)):
    """Get API usage statistics."""
    db = ensure_db()
    
    # Get user-specific stats
    job_stats = await get_job_stats(db, user["user_id"]) 
    total_users = await get_user_count(db)
    
    return StatsResponse(
        total_jobs=job_stats.get("total", 0),
        completed_jobs=job_stats.get("completed", 0),
        failed_jobs=job_stats.get("failed", 0),
        pending_jobs=job_stats.get("pending", 0),
        processing_jobs=job_stats.get("processing", 0),
        total_users=total_users
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
