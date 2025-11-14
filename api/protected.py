from fastapi import APIRouter, Request, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from typing import Optional
import uuid
import secrets
from datetime import datetime, timezone

from .config import settings
from .models import (
    OptimizeRequest,
    JobStatus,
    JobProgress,
    JobListResponse,
    UserResponse,
    APIKeyResponse,
    StatsResponse,
    JobStatusEnum,
)
from .database import (
    create_job,
    get_job,
    list_jobs,
    get_job_stats,
    update_job_status,
    get_google_tokens,
    get_user_by_id,
)
from .notifications import notify_job
from .auth import create_user_api_key
from .google_oauth import get_google_oauth_url, exchange_google_code, build_drive_service_for_user
from .constants import COOKIE_GOOGLE_OAUTH_STATE
from .app_logging import get_logger
from .exceptions import JobNotFoundError
from core.drive_utils import extract_folder_id_from_input
from .deps import (
    ensure_db,
    ensure_services,
    get_current_user,
    parse_job_progress,
)
from .utils import enqueue_job_with_guard

logger = get_logger(__name__)

router = APIRouter()


def _parse_job_progress_model(progress_str: str) -> JobProgress:
    data = parse_job_progress(progress_str) or {}
    return JobProgress(**data)


@router.get("/auth/github/status", tags=["Authentication"])
async def github_link_status(user: dict = Depends(get_current_user)):
    db = ensure_db()
    linked = bool(user.get("github_id"))
    github_id = user.get("github_id")
    email = user.get("email")
    if not linked:
        stored = await get_user_by_id(db, user["user_id"])  # type: ignore
        if stored:
            github_id = github_id or stored.get("github_id")
            email = email or stored.get("email")
            linked = bool(stored.get("github_id"))
    return {
        "linked": linked,
        "github_id": github_id,
        "email": email,
    }


@router.get("/auth/google/start", tags=["Authentication"])
async def google_auth_start(request: Request):
    try:
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
        logger.error(f"Google auth initiation failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Google OAuth not configured")


@router.get("/auth/google/callback", tags=["Authentication"])
async def google_auth_callback(code: str, state: str, request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()

    stored_state = request.cookies.get(COOKIE_GOOGLE_OAUTH_STATE)
    if not stored_state or not secrets.compare_digest(stored_state, state):
        logger.warning("Google OAuth state verification failed - possible CSRF attack")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid state parameter - possible CSRF attack")

    redirect_uri = request.cookies.get("google_redirect_uri") or str(request.url.replace(query=""))

    try:
        await exchange_google_code(db, user["user_id"], code, redirect_uri)
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
        response.delete_cookie(key="google_redirect_uri", path="/", samesite="lax", httponly=True, secure=is_secure)
        return response
    except Exception:
        logger.error("Google callback failed", exc_info=True)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google authentication failed") from None


@router.get("/auth/google/status", tags=["Authentication"])
async def google_link_status(user: dict = Depends(get_current_user)):
    db = ensure_db()
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    if not tokens:
        return {"linked": False}
    return {"linked": True, "expiry": tokens.get("expiry"), "scopes": tokens.get("scopes")}


@router.get("/auth/providers/status", tags=["Authentication"])
async def providers_status(user: dict = Depends(get_current_user)):
    db = ensure_db()
    # Determine GitHub linkage from user or DB
    github_linked = bool(user.get("github_id"))
    if not github_linked:
        stored = await get_user_by_id(db, user["user_id"])  # type: ignore
        github_linked = bool(stored and stored.get("github_id"))
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    google_linked = bool(tokens)
    return {
        "github_linked": github_linked,
        "google_linked": google_linked,
        "github_expiry": None,
        "github_scopes": None,
        "google_expiry": tokens.get("expiry") if tokens else None,
        "google_scopes": tokens.get("scopes") if tokens else None,
    }


@router.get("/auth/me", response_model=UserResponse, tags=["Authentication"])
async def get_current_user_info(user: dict = Depends(get_current_user)):
    created_at_val = user.get("created_at")
    created_at_dt = None
    if isinstance(created_at_val, datetime):
        created_at_dt = created_at_val if created_at_val.tzinfo else created_at_val.replace(tzinfo=timezone.utc)
    elif isinstance(created_at_val, str):
        try:
            dt = datetime.fromisoformat(created_at_val)
            created_at_dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            created_at_dt = None
    if created_at_dt is None:
        # Fetch canonical value from DB if not present/parsable in request state
        try:
            db = ensure_db()
            stored = await get_user_by_id(db, user["user_id"])  # type: ignore
            stored_created = stored.get("created_at") if stored else None
            if isinstance(stored_created, datetime):
                created_at_dt = stored_created if stored_created.tzinfo else stored_created.replace(tzinfo=timezone.utc)
            elif isinstance(stored_created, str) and stored_created:
                try:
                    dt2 = datetime.fromisoformat(stored_created)
                    created_at_dt = dt2 if dt2.tzinfo else dt2.replace(tzinfo=timezone.utc)
                except Exception:
                    created_at_dt = None
        except Exception:
            created_at_dt = None
        if created_at_dt is None:
            created_at_dt = datetime.now(timezone.utc)
    return UserResponse(
        user_id=user["user_id"],
        github_id=user.get("github_id"),
        email=user.get("email"),
        created_at=created_at_dt,
    )


@router.post("/auth/keys", response_model=APIKeyResponse, tags=["Authentication"])
async def create_api_key_endpoint(user: dict = Depends(get_current_user)):
    db = ensure_db()
    try:
        api_key = await create_user_api_key(db, user["user_id"])
        return APIKeyResponse(api_key=api_key, created_at=datetime.now(timezone.utc))
    except Exception as e:
        logger.error(f"API key creation failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create API key")


@router.post("/api/v1/optimize", response_model=JobStatus, tags=["Jobs"])
async def optimize_images(request: OptimizeRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    queue = ensure_services()[1]

    try:
        service = await build_drive_service_for_user(db, user["user_id"])  # type: ignore
        folder_id = extract_folder_id_from_input(request.drive_folder, service=service)
    except Exception:
        logger.error("Failed to prepare Drive service or extract folder id", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google not linked or folder not accessible")

    job_id = str(uuid.uuid4())
    try:
        # Persist the canonical folder_id so downstream consumers receive a validated ID
        job_data = await create_job(db, job_id, user["user_id"], folder_id, request.extensions)
        
        # Unified enqueue logic with environment-aware guard
        enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(
            queue, job_id, user["user_id"], request, allow_inline_fallback=False
        )
        
        if should_fail:
            # Production: queue required, fail if unavailable
            detail = "Queue unavailable or enqueue failed; background processing is required in production."
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
        
        # In development, if queue failed, we still return success but log warning
        # (API endpoint doesn't have BackgroundTasks fallback, so job will remain pending)
        if not enqueued:
            # Log the enqueue exception if available for better diagnostics
            logger.warning(
                f"Job {job_id} created but not enqueued. Job will remain in pending state.",
                extra={
                    "job_id": job_id,
                    "event": "job.enqueue_failed",
                    "reason": "queue unavailable or enqueue failed",
                    "enqueue_exception": (str(enqueue_exception) if enqueue_exception else None),
                },
            )
        
        logger.info(f"Created job {job_id} for user {user['user_id']}")
        progress = _parse_job_progress_model(progress_str=job_data.get("progress", "{}"))
        return JobStatus(
            job_id=job_id,
            user_id=user["user_id"],
            status=JobStatusEnum.PENDING,
            progress=progress,
            created_at=datetime.fromisoformat(job_data["created_at"]) if job_data.get("created_at") else datetime.now(timezone.utc),
            # Use canonical persisted folder id for consistency with storage
            drive_folder=(job_data.get("drive_folder") or folder_id),
        )
    except HTTPException:
        # Re-raise HTTP exceptions (like our 502 from should_fail)
        raise
    except Exception as e:
        logger.error(f"Failed to create job: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create optimization job")


@router.get("/api/v1/jobs/{job_id}", response_model=JobStatus, tags=["Jobs"])
async def get_job_status(job_id: str, user: dict = Depends(get_current_user)):
    db = ensure_db()
    job = await get_job(db, job_id, user["user_id"])
    if not job:
        raise JobNotFoundError(job_id)
    progress = _parse_job_progress_model(progress_str=job.get("progress", "{}"))
    return JobStatus(
        job_id=job["job_id"],
        user_id=job["user_id"],
        status=JobStatusEnum(job["status"]),
        progress=progress,
        created_at=datetime.fromisoformat(job["created_at"]),
        completed_at=datetime.fromisoformat(job["completed_at"]) if job.get("completed_at") else None,
        error=job.get("error"),
        drive_folder=job.get("drive_folder"),
    )


@router.get("/api/v1/jobs", response_model=JobListResponse, tags=["Jobs"])
async def list_user_jobs(page: int = 1, page_size: int = 20, status_filter: Optional[JobStatusEnum] = None, user: dict = Depends(get_current_user)):
    db = ensure_db()
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20
    jobs_list, total = await list_jobs(db, user["user_id"], page=page, page_size=page_size, status=status_filter.value if status_filter else None)
    job_statuses = []
    for job in jobs_list:
        progress = _parse_job_progress_model(progress_str=job.get("progress", "{}"))
        job_statuses.append(
            JobStatus(
                job_id=job["job_id"],
                user_id=job["user_id"],
                status=JobStatusEnum(job["status"]),
                progress=progress,
                created_at=datetime.fromisoformat(job["created_at"]),
                completed_at=datetime.fromisoformat(job["completed_at"]) if job.get("completed_at") else None,
                error=job.get("error"),
                drive_folder=job.get("drive_folder"),
            )
        )
    return JobListResponse(jobs=job_statuses, total=total, page=page, page_size=page_size, has_more=(page * page_size) < total)


@router.delete("/api/v1/jobs/{job_id}", tags=["Jobs"])
async def cancel_job(job_id: str, user: dict = Depends(get_current_user)):
    db = ensure_db()
    job = await get_job(db, job_id, user["user_id"])
    if not job:
        raise JobNotFoundError(job_id)
    current_status = JobStatusEnum(job["status"])
    if current_status in [JobStatusEnum.COMPLETED, JobStatusEnum.FAILED, JobStatusEnum.CANCELLED]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot cancel job with status: {current_status.value}")
    await update_job_status(db, job_id, "cancelled")
    try:
        await notify_job(db, user_id=user["user_id"], job_id=job_id, level="error", text=f"Job {job_id} cancelled")
    except Exception:
        pass
    logger.info(f"Cancelled job {job_id} for user {user['user_id']}")
    return {"ok": True, "job_id": job_id}


@router.get("/api/v1/stats", response_model=StatsResponse, tags=["Stats"])
async def get_stats(user: dict = Depends(get_current_user)):
    db = ensure_db()
    job_stats = await get_job_stats(db, user["user_id"]) 
    return StatsResponse(
        total_jobs=job_stats.get("total", 0),
        completed_jobs=job_stats.get("completed", 0),
        failed_jobs=job_stats.get("failed", 0),
        pending_jobs=job_stats.get("pending", 0),
        processing_jobs=job_stats.get("processing", 0),
        total_users=None,
    )
