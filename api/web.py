def _status_label(value: str) -> str:
    mapping = {
        "processing": "Running",
        "pending": "Queued",
        "queued": "Queued",
        "completed": "Completed",
        "failed": "Failed",
        "cancelled": "Cancelled",
    }
    return mapping.get(value, (value or "").title())

from fastapi import APIRouter, Request, Depends, Form, HTTPException, status, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.templating import Jinja2Templates
from starlette.responses import Response, StreamingResponse
from typing import Optional
import os
import uuid
import logging
import json
import secrets
import hmac

from .models import OptimizeRequest, JobStatusEnum
from .deps import ensure_services, ensure_db, parse_job_progress, get_current_user
from .auth import verify_jwt_token
from .database import (
    create_job,
    list_jobs,
    get_job_stats,
    get_google_tokens,
    get_job,
    update_job_status,
    delete_google_tokens,
    create_notification,
    list_notifications,
    mark_notification_seen,
    dismiss_notification,
    get_user_by_id,
)
from .auth import create_user_api_key
from .protected import get_job_status as protected_get_job_status
from workers.consumer import process_optimization_job
from .config import settings
from .notifications import notify_job
from .notifications_stream import notifications_stream_response
from .constants import COOKIE_OAUTH_STATE, COOKIE_GOOGLE_OAUTH_STATE
from .utils import normalize_ui_status, enqueue_job_with_guard

logger = logging.getLogger(__name__)

router = APIRouter()

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Register Jinja filter once (after templates is initialized)
templates.env.filters["status_label"] = _status_label

# Centralized service metadata used across integrations views
SERVICES_META = {
    "gmail": {
        "key": "gmail",
        "name": "Gmail",
        "capability": "Alerts, approvals",
        "description": "Send status alerts and approvals directly from Gmail.",
        "category": "Email",
        "developer": "Google",
        "website": "https://mail.google.com/",
        "privacy": "https://policies.google.com/privacy",
        "created_at": None,
    },
    "drive": {
        "key": "drive",
        "name": "Google Drive",
        "capability": "File uploads",
        "description": "Sync folders and enqueue conversions without exporting files.",
        "category": "Storage",
        "developer": "Google",
        "website": "https://drive.google.com/",
        "privacy": "https://policies.google.com/privacy",
        "created_at": None,
    },
    "youtube": {
        "key": "youtube",
        "name": "YouTube",
        "capability": "Media",
        "description": "Convert thumbnails or channel assets via queued jobs.",
        "category": "Media",
        "developer": "Google",
        "website": "https://youtube.com/",
        "privacy": "https://policies.google.com/privacy",
        "created_at": None,
    },
}

def _get_csrf_token(request: Request) -> str:
    token = request.cookies.get("csrf_token")
    if not token:
        token = uuid.uuid4().hex
    return token


def _is_secure_request(request: Request) -> bool:
    xf_proto = request.headers.get("x-forwarded-proto", "").lower()
    if xf_proto:
        return xf_proto == "https"
    return request.url.scheme == "https"


def _render_auth_page(request: Request, view_mode: str) -> Response:
    """Render shared login/signup template with CSRF setup."""
    if getattr(request.state, "user", None):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    csrf = _get_csrf_token(request)
    context = {"request": request, "csrf_token": csrf, "view_mode": view_mode}
    resp = templates.TemplateResponse("auth/login.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = _is_secure_request(request)
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/api/notifications")
async def api_list_notifications(request: Request, user: dict = Depends(get_current_user), after_id: Optional[str] = None, limit: int = 50):
    db = ensure_db()
    notifs = await list_notifications(db, user["user_id"], after_id=after_id, limit=min(max(limit, 1), 100))
    return JSONResponse({"notifications": notifs}, headers={"Cache-Control": "no-store"})


@router.post("/api/notifications/{notification_id}/seen")
async def api_mark_seen(notification_id: str, request: Request, user: dict = Depends(get_current_user)):
    # CSRF protection via header token for HTMX
    cookie_token = request.cookies.get("csrf_token")
    header_token = request.headers.get("X-CSRF-Token")
    if cookie_token is None or header_token is None or not hmac.compare_digest(str(cookie_token), str(header_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    await mark_notification_seen(db, user["user_id"], notification_id)
    return {"ok": True}


@router.post("/api/notifications/{notification_id}/dismiss")
async def api_dismiss(notification_id: str, request: Request, user: dict = Depends(get_current_user)):
    # CSRF protection via header token for HTMX
    cookie_token = request.cookies.get("csrf_token")
    header_token = request.headers.get("X-CSRF-Token")
    if cookie_token is None or header_token is None or not hmac.compare_digest(str(cookie_token), str(header_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    await dismiss_notification(db, user["user_id"], notification_id)
    return {"ok": True}


@router.get("/api/stream")
async def api_stream(request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    return notifications_stream_response(request, db, user)


@router.get("/dashboard/activity", response_class=HTMLResponse)
async def activity_page(request: Request, user: dict = Depends(get_current_user), after_id: Optional[str] = None):
    db = ensure_db()
    notifs = await list_notifications(db, user["user_id"], after_id=after_id, limit=50)
    return templates.TemplateResponse("activity/index.html", {"request": request, "user": user, "notifications": notifs, "page_title": "Activity"})


@router.post("/auth/logout")
async def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    """Clear auth/session cookies and redirect to login."""
    # Validate CSRF token for security (constant-time comparison)
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or not secrets.compare_digest(cookie_token, csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    
    # Determine secure flag consistently with how cookies were set
    is_secure = _is_secure_request(request)
    
    # Clear all authentication-related cookies with matching attributes
    response.delete_cookie("access_token", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie(COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie(COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_redirect_uri", path="/", samesite="lax", httponly=True, secure=is_secure)
    # Clear CSRF token on logout for security
    response.delete_cookie("csrf_token", path="/", samesite="lax", httponly=True, secure=is_secure)
    
    return response


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if getattr(request.state, "user", None):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("home.html", {"request": request})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _render_auth_page(request, view_mode="login")


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return _render_auth_page(request, view_mode="signup")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, page: int = 1, user: dict = Depends(get_current_user)):
    
    db = ensure_db()
    jobs_list, total = await list_jobs(db, user["user_id"], page=page, page_size=10, status=None)
    stats = await get_job_stats(db, user["user_id"])  # { total, completed, failed, pending, processing }
    csrf = _get_csrf_token(request)
    # Google connection status for dashboard badge
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    google_connected = bool(tokens)

    # Transform jobs for dashboard table
    def to_view(j):
        status = j.get("status", "queued")
        return {
            "id": j.get("job_id"),
            "kind": "Drive",
            "status": status,
            "status_label": _status_label(status),
            "created_at": j.get("created_at"),
        }

    context = {
        "request": request,
        "user": user,
        "jobs": [to_view(j) for j in jobs_list],
        "stats": {
            "queued": stats.get("pending", 0),
            "running": stats.get("processing", 0),
            "completed": stats.get("completed", 0),
        },
        "csrf_token": csrf,
        "page_title": "Dashboard",
        "google_connected": google_connected,
    }
    resp = templates.TemplateResponse("dashboard/index.html", context)
    if not request.cookies.get("csrf_token"):
        # Set secure cookies only in production or when the request is over HTTPS
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.post("/dashboard/jobs", response_class=HTMLResponse)
async def create_job_html(
    request: Request,
    background_tasks: BackgroundTasks,
    drive_url: str = Form(...),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or cookie_token != csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    db, queue = ensure_services()

    folder_input = (drive_url or "").strip()
    if not folder_input:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Drive URL required")

    job_id = str(uuid.uuid4())

    # Build request model allowing defaults to apply (including default extensions whitelist)
    req_model = OptimizeRequest(drive_folder=folder_input)
    extensions_list = req_model.extensions if req_model.extensions is not None else []

    # Persist job with concrete extensions list (not None)
    await create_job(db, job_id, user["user_id"], folder_input, extensions_list)
    
    # Unified enqueue logic with environment-aware guard
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(
        queue, job_id, user["user_id"], req_model, allow_inline_fallback=True
    )
    
    if should_fail:
        # Production: queue required, fail if unavailable
        detail = "Queue unavailable or enqueue failed; background processing is required in production."
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    
    # Development: allow inline fallback when queue missing or enqueue failed
    if not enqueued:
        try:
            logger.info(
                f"Using BackgroundTasks fallback for job {job_id}",
                extra={
                    "job_id": job_id,
                    "user_id": user["user_id"],
                    "event": "job.fallback_background_tasks",
                    "environment": settings.environment,
                    "fallback_type": "BackgroundTasks"
                }
            )
            background_tasks.add_task(
                process_optimization_job,
                db=db,
                job_id=job_id,
                user_id=user["user_id"],
                drive_folder=folder_input,
                extensions=extensions_list,
                overwrite=req_model.overwrite,
                skip_existing=req_model.skip_existing,
                cleanup_originals=req_model.cleanup_originals,
                max_retries=req_model.max_retries,
            )
        except Exception as e:
            logger.error(
                f"Failed to schedule inline job {job_id}: {e}",
                exc_info=True,
                extra={
                    "job_id": job_id,
                    "user_id": user["user_id"],
                    "event": "job.fallback_failed",
                    "fallback_type": "BackgroundTasks",
                    "error": str(e)
                }
            )
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to schedule job")

    jobs_list, total = await list_jobs(db, user["user_id"], page=1, page_size=10, status=None)

    # Transform for partial
    def to_view(j):
        status = j.get("status", "queued")
        return {
            "id": j.get("job_id"),
            "kind": "Drive",
            "status": status,
            "status_label": _status_label(status),
            "created_at": j.get("created_at"),
        }

    return templates.TemplateResponse(
        "jobs/partials/jobs_list.html",
        {"request": request, "jobs": [to_view(j) for j in jobs_list]},
    )


@router.get("/dashboard/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_partial(job_id: str, request: Request, user: dict = Depends(get_current_user)):
    data = await protected_get_job_status(job_id, user)  # reuse existing handler logic
    progress = parse_job_progress(data.progress.model_dump_json() if hasattr(data.progress, "model_dump_json") else "{}")
    csrf = _get_csrf_token(request)
    context = {
        "request": request,
        "job": {
            "job_id": data.job_id,
            "id": data.job_id,
            "status": data.status.value if hasattr(data.status, "value") else str(data.status),
            "status_label": _status_label(data.status.value if hasattr(data.status, "value") else str(data.status)),
            "error": data.error,
            "drive_folder": data.drive_folder,
            "progress": progress,
            "created_at": data.created_at,
            "events": [],
        },
        "csrf_token": csrf,
        "user": user,
    }
    resp = templates.TemplateResponse("jobs/detail.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = _is_secure_request(request)
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/dashboard/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, user: dict = Depends(get_current_user), page: int = 1, status: Optional[str] = None):
    db = ensure_db()
    csrf = _get_csrf_token(request)
    status_filter = None
    if status:
        try:
            norm = normalize_ui_status(status)
            status_filter = JobStatusEnum(norm) if norm else None
        except Exception:
            status_filter = None
    jobs_list: list[dict] = []
    total = 0
    stats: dict = {}
    load_error: Optional[str] = None
    try:
        jobs_list, total = await list_jobs(
            db,
            user["user_id"],
            page=page,
            page_size=20,
            status=(status_filter.value if status_filter else None),
        )
        stats = await get_job_stats(db, user["user_id"])
    except Exception as exc:
        logger.exception("Failed loading jobs list: %s", exc)
        load_error = "We couldn't load your jobs right now. Please refresh in a moment."

    def to_view(j):
        st = j.get("status", "queued")
        return {
            "id": j.get("job_id"),
            "kind": "Drive",
            "status": st,
            "status_label": _status_label(st),
            "created_at": j.get("created_at"),
        }

    context = {
        "request": request,
        "user": user,
        "jobs": [to_view(j) for j in jobs_list],
        "total": total,
        "page": page,
        "page_title": "Jobs",
        "current_status": (status_filter.value if status_filter else None),
        "csrf_token": csrf,
        "stats": stats or {},
        "load_error": load_error,
    }
    resp = templates.TemplateResponse("jobs/index.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/dashboard/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    google_connected = bool(tokens)
    integrations = {
        "gmail": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
        "drive": {"connected": google_connected, "status": ("completed" if google_connected else "disconnected"), "status_label": ("Connected" if google_connected else "Disconnected")},
        "youtube": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
    }
    services_meta = SERVICES_META
    csrf = _get_csrf_token(request)
    context = {"request": request, "user": user, "integrations": integrations, "services_meta": services_meta, "page_title": "Integrations", "csrf_token": csrf}
    resp = templates.TemplateResponse("integrations/index.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.post("/dashboard/integrations/drive/disconnect")
async def integrations_drive_disconnect(request: Request, csrf_token: str = Form(...), user: dict = Depends(get_current_user)):
    cookie_token = request.cookies.get("csrf_token")
    if cookie_token is None or csrf_token is None or not hmac.compare_digest(str(cookie_token), str(csrf_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    await delete_google_tokens(db, user["user_id"])  # type: ignore
    return RedirectResponse(url="/dashboard/integrations", status_code=status.HTTP_302_FOUND)


@router.get("/dashboard/integrations/partials/grid", response_class=HTMLResponse)
async def integrations_grid_partial(request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    google_connected = bool(tokens)
    integrations = {
        "gmail": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
        "drive": {"connected": google_connected, "status": ("completed" if google_connected else "disconnected"), "status_label": ("Connected" if google_connected else "Disconnected")},
        "youtube": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
    }
    services_meta = SERVICES_META
    csrf = _get_csrf_token(request)
    return templates.TemplateResponse("integrations/partials/grid.html", {"request": request, "integrations": integrations, "services_meta": services_meta, "csrf_token": csrf})


@router.get("/dashboard/integrations/{service}", response_class=HTMLResponse)
async def integration_detail(service: str, request: Request, user: dict = Depends(get_current_user)):
    # Validate service early to avoid unnecessary DB calls and object construction
    allowed_services = {"gmail", "drive", "youtube"}
    if service not in allowed_services:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    db = ensure_db()
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    google_connected = bool(tokens)
    integrations = {
        "gmail": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
        "drive": {"connected": google_connected, "status": ("completed" if google_connected else "disconnected"), "status_label": ("Connected" if google_connected else "Disconnected")},
        "youtube": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
    }
    services_meta = SERVICES_META
    csrf = _get_csrf_token(request)
    return templates.TemplateResponse(
        "integrations/detail.html",
        {"request": request, "user": user, "service": services_meta[service], "integration": integrations.get(service), "csrf_token": csrf}
    )


@router.post("/dashboard/jobs/{job_id}/retry")
async def retry_job(job_id: str, request: Request, user: dict = Depends(get_current_user)):
    db, queue = ensure_services()
    # CSRF protection via header token for HTMX
    cookie_token = request.cookies.get("csrf_token")
    header_token = request.headers.get("X-CSRF-Token")
    if cookie_token is None or header_token is None or not secrets.compare_digest(str(cookie_token), str(header_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    job = await get_job(db, job_id, user["user_id"])  # type: ignore
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    # Reset status to pending, clear error, and optionally reset progress
    try:
        await update_job_status(
            db,
            job_id,
            "pending",
            progress={"stage": "queued", "downloaded": 0, "optimized": 0, "skipped": 0, "uploaded": 0, "deleted": 0, "download_failed": 0, "upload_failed": 0, "recent_logs": []},
            error="",
        )
    except Exception:
        # If progress reset fails for any reason, at least clear status/error
        await update_job_status(db, job_id, "pending", error="")
    
    # Unified enqueue logic with environment-aware guard
    extensions_raw = job.get("extensions")
    extensions_list = []
    try:
        if isinstance(extensions_raw, str):
            parsed = json.loads(extensions_raw)
            if isinstance(parsed, list):
                extensions_list = parsed
        elif isinstance(extensions_raw, list):
            extensions_list = extensions_raw
    except Exception:
        extensions_list = []
    req_model = OptimizeRequest(drive_folder=job.get("drive_folder"), extensions=extensions_list)
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(
        queue, job_id, user["user_id"], req_model, allow_inline_fallback=False
    )
    
    if should_fail:
        # Production: queue required, fail if unavailable
        # Return HTMX-friendly error response
        return Response(
            content="Queue unavailable or enqueue failed; background processing is required in production.",
            status_code=status.HTTP_502_BAD_GATEWAY,
            media_type="text/plain"
        )
    
    # In development, if queue failed, log warning but still return success
    # (retry endpoint doesn't have BackgroundTasks fallback)
    if not enqueued:
        logger.warning(
            f"Job {job_id} retry: queue unavailable. Job will remain in pending state.",
            extra={
                "job_id": job_id,
                "event": "job.retry_enqueue_failed",
                "enqueue_exception": (str(enqueue_exception) if enqueue_exception else None),
            },
        )
    
    # Notify
    try:
        await notify_job(db, user_id=user["user_id"], job_id=job_id, level="info", text=f"Job {job_id} retried")
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/dashboard/jobs/{job_id}")
async def cancel_job_html(job_id: str, request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    # CSRF protection via header token for HTMX
    cookie_token = request.cookies.get("csrf_token")
    header_token = request.headers.get("X-CSRF-Token")
    if cookie_token is None or header_token is None or not secrets.compare_digest(str(cookie_token), str(header_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    job = await get_job(db, job_id, user["user_id"])  # type: ignore
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    # Check if job can be cancelled (same logic as API endpoint)
    current_status = JobStatusEnum(job["status"])
    if current_status in [JobStatusEnum.COMPLETED, JobStatusEnum.FAILED, JobStatusEnum.CANCELLED]:
        # Return HTMX-friendly error response
        return Response(
            content=f"Cannot cancel job with status: {current_status.value}",
            status_code=status.HTTP_400_BAD_REQUEST,
            media_type="text/plain"
        )
    await update_job_status(db, job_id, "cancelled")
    # Notify
    try:
        await notify_job(db, user_id=user["user_id"], job_id=job_id, level="error", text=f"Job {job_id} cancelled")
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/dashboard/integrations/{service}/connect")
async def integration_connect_stub(service: str, request: Request, user: dict = Depends(get_current_user)):
    """Stub connect for non-Drive services."""
    if service not in {"gmail", "youtube"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=f"{service} connect not implemented yet")


@router.post("/dashboard/integrations/{service}/disconnect")
async def integration_disconnect_stub(service: str, request: Request, user: dict = Depends(get_current_user)):
    """Stub disconnect for non-Drive services."""
    if service not in {"gmail", "youtube"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=f"{service} disconnect not implemented yet")


@router.get("/dashboard/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: dict = Depends(get_current_user)):
    csrf = _get_csrf_token(request)
    context = {"request": request, "user": user, "page_title": "Settings", "csrf_token": csrf}
    resp = templates.TemplateResponse("settings/index.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.post("/dashboard/settings/api-key", response_class=HTMLResponse)
async def settings_api_key(request: Request, csrf_token: str = Form(...), user: dict = Depends(get_current_user)):
    cookie_token = request.cookies.get("csrf_token")
    if cookie_token is None or not secrets.compare_digest(str(cookie_token), str(csrf_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    api_key = await create_user_api_key(db, user["user_id"])  # type: ignore
    return templates.TemplateResponse("settings_api_key_result.html", {"request": request, "api_key": api_key})


@router.get("/dashboard/account", response_class=HTMLResponse)
async def account_page(request: Request, user: dict = Depends(get_current_user)):
    csrf = _get_csrf_token(request)
    db = ensure_db()
    stored = None
    try:
        stored = await get_user_by_id(db, user["user_id"])  # type: ignore
    except Exception:
        stored = None
    display_user = {
        "user_id": user.get("user_id"),
        "github_id": user.get("github_id") or (stored.get("github_id") if stored else None),
        "email": user.get("email") or (stored.get("email") if stored else None),
        "created_at": stored.get("created_at") if stored else None,
    }
    context = {"request": request, "user": display_user, "page_title": "Account", "csrf_token": csrf}
    resp = templates.TemplateResponse("account/index.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp
