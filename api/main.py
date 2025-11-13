"""
Production-ready FastAPI web application for Google Drive Image Optimizer.
"""

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware as FastAPICORSMiddleware
from contextlib import asynccontextmanager
from typing import Optional
import uuid
import re
from datetime import datetime

from .config import settings
from .models import (
    OptimizeRequest,
    JobStatus,
    JobProgress,
    JobListResponse,
    UserResponse,
    APIKeyResponse,
    ErrorResponse,
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
    get_user_by_id
)
from .auth import (
    authenticate_github,
    create_user_api_key,
    get_github_oauth_url
)
from .cloudflare_queue import QueueProducer
from .exceptions import (
    APIException,
    JobNotFoundError,
    NotFoundError,
    ValidationError
)
from .app_logging import setup_logging, get_logger, get_request_id
from .middleware import (
    RequestIDMiddleware,
    AuthenticationMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    CORSMiddleware
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
    
    # Initialize queue producer
    queue_producer = QueueProducer(queue=settings.queue)
    app_logger.info("Queue producer initialized")
    
    # Add authentication middleware after db is initialized
    # Note: This is a workaround - in production, middleware should be added before app creation
    # For now, we'll handle auth in dependencies instead
    
    yield
    
    # Shutdown
    app_logger.info("Shutting down application")


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    description="Production-ready API for optimizing images from Google Drive to WebP format",
    version=settings.app_version,
    lifespan=lifespan
)

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


# Dependency to get current user
async def get_current_user(request: Request) -> dict:
    """Get current authenticated user from request state."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    return user


# Utility functions
def extract_folder_id_from_input(folder_input: str) -> str:
    """Extract folder ID from share link or return as-is if already an ID."""
    match = re.search(r"/folders/([\w-]+)", folder_input)
    if match:
        return match.group(1)
    if re.match(r"^[\w-]{10,}$", folder_input):
        return folder_input
    raise ValidationError("Invalid Google Drive folder link or ID.")


# Public endpoints
@app.get("/", tags=["Public"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "endpoints": {
            "auth": "/auth/github",
            "optimize": "/api/v1/optimize",
            "jobs": "/api/v1/jobs",
            "health": "/health",
            "docs": "/docs"
        }
    }


@app.get("/health", response_model=HealthResponse, tags=["Public"])
async def health():
    """Health check endpoint."""
    health_data = {
        "status": "healthy",
        "version": settings.app_version,
        "timestamp": datetime.utcnow()
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


# Authentication endpoints
@app.get("/auth/github", tags=["Authentication"])
async def github_auth():
    """Initiate GitHub OAuth flow."""
    try:
        auth_url = get_github_oauth_url()
        return RedirectResponse(url=auth_url)
    except Exception as e:
        app_logger.error(f"GitHub auth initiation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub OAuth not configured"
        )


@app.get("/auth/callback", tags=["Authentication"])
async def github_callback(code: str, request: Request):
    """Handle GitHub OAuth callback."""
    if not db_instance:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database not initialized"
        )
    
    try:
        jwt_token, user = await authenticate_github(db_instance, code)
        
        # Redirect to frontend with token (in production, use secure cookie)
        # For now, return token in response
        return {
            "access_token": jwt_token,
            "token_type": "bearer",
            "user": {
                "user_id": user["user_id"],
                "email": user.get("email"),
                "github_id": user.get("github_id")
            }
        }
    except Exception as e:
        app_logger.error(f"GitHub callback failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(e)}"
        )


@app.post("/auth/api-key", response_model=APIKeyResponse, tags=["Authentication"])
async def create_api_key_endpoint(user: dict = Depends(get_current_user)):
    """Generate a new API key for the authenticated user."""
    if not db_instance:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database not initialized"
        )
    
    try:
        api_key = await create_user_api_key(db_instance, user["user_id"])
        return APIKeyResponse(
            api_key=api_key,
            created_at=datetime.utcnow()
        )
    except Exception as e:
        app_logger.error(f"API key creation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create API key"
        )


@app.get("/auth/me", response_model=UserResponse, tags=["Authentication"])
async def get_current_user_info(user: dict = Depends(get_current_user)):
    """Get current authenticated user information."""
    return UserResponse(
        user_id=user["user_id"],
        github_id=user.get("github_id"),
        email=user.get("email"),
        created_at=datetime.fromisoformat(user["created_at"]) if user.get("created_at") else datetime.utcnow()
    )


# Job endpoints
@app.post("/api/v1/optimize", response_model=JobStatus, tags=["Jobs"])
async def optimize_images(
    request: OptimizeRequest,
    user: dict = Depends(get_current_user)
):
    """Start an image optimization job."""
    if not db_instance or not queue_producer:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Service not fully initialized"
        )
    
    # Validate folder ID
    try:
        folder_id = extract_folder_id_from_input(request.drive_folder)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e)
        )
    
    # Create job
    job_id = str(uuid.uuid4())
    try:
        job_data = await create_job(
            db_instance,
            job_id,
            user["user_id"],
            request.drive_folder,
            request.extensions
        )
        
        # Send to queue
        await queue_producer.send_job(job_id, user["user_id"], request)
        
        app_logger.info(f"Created job {job_id} for user {user['user_id']}")
        
        # Return job status
        progress = JobProgress(stage="queued")
        return JobStatus(
            job_id=job_id,
            user_id=user["user_id"],
            status=JobStatusEnum.PENDING,
            progress=progress,
            created_at=datetime.fromisoformat(job_data["created_at"]) if job_data.get("created_at") else datetime.utcnow(),
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
    if not db_instance:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database not initialized"
        )
    
    job = await get_job(db_instance, job_id, user["user_id"])
    if not job:
        raise JobNotFoundError(job_id)
    
    import json
    progress_data = json.loads(job.get("progress", "{}"))
    progress = JobProgress(**progress_data)
    
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
    if not db_instance:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database not initialized"
        )
    
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20
    
    jobs_list, total = await list_jobs(
        db_instance,
        user["user_id"],
        page=page,
        page_size=page_size,
        status=status_filter.value if status_filter else None
    )
    
    import json
    job_statuses = []
    for job in jobs_list:
        progress_data = json.loads(job.get("progress", "{}"))
        progress = JobProgress(**progress_data)
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
    if not db_instance:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database not initialized"
        )
    
    job = await get_job(db_instance, job_id, user["user_id"])
    if not job:
        raise JobNotFoundError(job_id)
    
    current_status = job["status"]
    if current_status in ["completed", "failed", "cancelled"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel job with status: {current_status}"
        )
    
    from database import update_job_status
    await update_job_status(db_instance, job_id, "cancelled")
    
    app_logger.info(f"Cancelled job {job_id} for user {user['user_id']}")
    
    return {"message": "Job cancelled successfully", "job_id": job_id}


# Admin/Stats endpoints
@app.get("/api/v1/stats", response_model=StatsResponse, tags=["Admin"])
async def get_stats(user: dict = Depends(get_current_user)):
    """Get API usage statistics."""
    if not db_instance:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database not initialized"
        )
    
    # Get user-specific stats
    job_stats = await get_job_stats(db_instance, user["user_id"])
    total_users = await get_user_count(db_instance)
    
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
