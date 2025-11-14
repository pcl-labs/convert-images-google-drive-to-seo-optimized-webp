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
)
from .auth import create_user_api_key
from workers.consumer import process_optimization_job
from .config import settings
from .utils import normalize_ui_status
from .notifications import notify_job
from .notifications_stream import notifications_stream_response
from .constants import COOKIE_OAUTH_STATE, COOKIE_GOOGLE_OAUTH_STATE
from .utils import normalize_ui_status

logger = logging.getLogger(__name__)

router = APIRouter()

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Register Jinja filter once (after templates is initialized)
templates.env.filters["status_label"] = _status_label


def _get_csrf_token(request: Request) -> str:
    token = request.cookies.get("csrf_token")
    if not token:
        token = uuid.uuid4().hex
    return token


 


@router.get("/api/notifications")
async def api_list_notifications(request: Request, user: dict = Depends(get_current_user), after_id: Optional[str] = None, limit: int = 50):
    db = ensure_db()
    notifs = await list_notifications(db, user["user_id"], after_id=after_id, limit=min(max(limit, 1), 100))
    return JSONResponse({"notifications": notifs}, headers={"Cache-Control": "no-store"})


@router.post("/api/notifications/{notification_id}/seen")
async def api_mark_seen(notification_id: str, request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    await mark_notification_seen(db, user["user_id"], notification_id)
    return {"ok": True}


@router.post("/api/notifications/{notification_id}/dismiss")
async def api_dismiss(notification_id: str, request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    await dismiss_notification(db, user["user_id"], notification_id)
    return {"ok": True}


@router.get("/api/stream")
async def api_stream(request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    return notifications_stream_response(request, db, user)


@router.get("/activity", response_class=HTMLResponse)
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
    
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    # Clear all authentication-related cookies
    response.delete_cookie("access_token", path="/", samesite="lax")
    response.delete_cookie(COOKIE_OAUTH_STATE, path="/", samesite="lax")
    response.delete_cookie(COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax")
    response.delete_cookie("google_redirect_uri", path="/", samesite="lax")
    # Clear CSRF token on logout for security
    response.delete_cookie("csrf_token", path="/", samesite="lax")
    
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # If already authenticated via JWT cookie, go straight to dashboard
    token = request.cookies.get("access_token")
    if token:
        try:
            verify_jwt_token(token)
            return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        except Exception:
            pass
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("auth/login.html", {"request": request, "csrf_token": csrf})
    if not request.cookies.get("csrf_token"):
        # Set secure cookies only in production or when the request is over HTTPS
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(get_current_user), page: int = 1):
    db = ensure_db()
    jobs_list, total = await list_jobs(db, user["user_id"], page=page, page_size=10, status=None)
    stats = await get_job_stats(db, user["user_id"])  # { total, completed, failed, pending, processing }
    csrf = _get_csrf_token(request)

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
    }
    resp = templates.TemplateResponse("dashboard/index.html", context)
    if not request.cookies.get("csrf_token"):
        # Set secure cookies only in production or when the request is over HTTPS
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.post("/jobs", response_class=HTMLResponse)
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
    enqueued = False
    enqueue_exception: Optional[Exception] = None
    # Only attempt to enqueue when a queue backend is configured
    if getattr(queue, "queue", None) is not None:
        try:
            enqueued = await queue.send_job(job_id, user["user_id"], req_model)
        except Exception as e:
            # Do not assume not enqueued; avoid immediate inline execution
            enqueue_exception = e
            logger.error(f"Failed to enqueue job {job_id}: {e}", exc_info=True)

    # If no queue configured or enqueue definitively returned False (without exception), run inline via BackgroundTasks
    should_run_inline = (getattr(queue, "queue", None) is None) or (not enqueued and enqueue_exception is None)
    if should_run_inline:
        try:
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
            logger.error(f"Failed to schedule inline job {job_id}: {e}", exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to schedule job")

    # If enqueue raised an exception and we chose not to run inline to avoid duplicates,
    # surface the failure so the user isn't shown success with no processing.
    if enqueue_exception is not None and not should_run_inline:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to enqueue job; please retry shortly")

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


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_partial(job_id: str, request: Request, user: dict = Depends(get_current_user)):
    from .main import get_job_status
    data = await get_job_status(job_id, user)  # reuse existing handler logic
    progress = parse_job_progress(data.progress.model_dump_json() if hasattr(data.progress, "model_dump_json") else "{}")
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
    }
    return templates.TemplateResponse("jobs/detail.html", context)


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, user: dict = Depends(get_current_user), page: int = 1, status: Optional[str] = None):
    db = ensure_db()
    status_filter = None
    if status:
        try:
            norm = normalize_ui_status(status)
            status_filter = JobStatusEnum(norm) if norm else None
        except Exception:
            status_filter = None
    jobs_list, total = await list_jobs(db, user["user_id"], page=page, page_size=20, status=(status_filter.value if status_filter else None))

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
    }
    return templates.TemplateResponse("jobs/index.html", context)


@router.get("/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    google_connected = bool(tokens)
    integrations = {
        "gmail": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
        "drive": {"connected": google_connected, "status": ("completed" if google_connected else "disconnected"), "status_label": ("Connected" if google_connected else "Disconnected")},
        "youtube": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
    }
    csrf = _get_csrf_token(request)
    context = {"request": request, "user": user, "integrations": integrations, "page_title": "Integrations", "csrf_token": csrf}
    resp = templates.TemplateResponse("integrations/index.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.post("/integrations/drive/disconnect")
async def integrations_drive_disconnect(request: Request, csrf_token: str = Form(...), user: dict = Depends(get_current_user)):
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or cookie_token != csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    await delete_google_tokens(db, user["user_id"])  # type: ignore
    return RedirectResponse(url="/integrations", status_code=status.HTTP_302_FOUND)


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, request: Request, user: dict = Depends(get_current_user)):
    db, queue = ensure_services()
    job = await get_job(db, job_id, user["user_id"])  # type: ignore
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    # Reset status to pending and clear error
    await update_job_status(db, job_id, "pending")
    try:
        req_model = OptimizeRequest(drive_folder=job.get("drive_folder"), extensions=job.get("extensions") or [])
        if getattr(queue, "queue", None) is not None:
            await queue.send_job(job_id, user["user_id"], req_model)
    except Exception:
        # Swallow queue errors for UI; background may be unavailable
        pass
    # Notify
    try:
        await notify_job(db, user_id=user["user_id"], job_id=job_id, level="info", text=f"Job {job_id} retried")
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/jobs/{job_id}")
async def cancel_job_html(job_id: str, request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    job = await get_job(db, job_id, user["user_id"])  # type: ignore
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    await update_job_status(db, job_id, "cancelled")
    # Notify
    try:
        await notify_job(db, user_id=user["user_id"], job_id=job_id, level="error", text=f"Job {job_id} cancelled")
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/integrations/{service}/connect")
async def integration_connect_stub(service: str, request: Request, user: dict = Depends(get_current_user)):
    """Stub connect for non-Drive services."""
    if service not in {"gmail", "youtube"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=f"{service} connect not implemented yet")


@router.post("/integrations/{service}/disconnect")
async def integration_disconnect_stub(service: str, request: Request, user: dict = Depends(get_current_user)):
    """Stub disconnect for non-Drive services."""
    if service not in {"gmail", "youtube"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=f"{service} disconnect not implemented yet")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: dict = Depends(get_current_user)):
    csrf = _get_csrf_token(request)
    context = {"request": request, "user": user, "page_title": "Settings", "csrf_token": csrf}
    resp = templates.TemplateResponse("settings/index.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.post("/settings/api-key", response_class=HTMLResponse)
async def generate_api_key(request: Request, csrf_token: str = Form(...), user: dict = Depends(get_current_user)):
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or cookie_token != csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    key = await create_user_api_key(db, user["user_id"])  # type: ignore
    html = f"""
    <div class=\"bg-slate-900/60 border border-slate-800 rounded-md p-3\">
      <div class=\"text-slate-300 text-sm\">New API Key</div>
      <div class=\"mt-1 font-mono text-white break-all\">{key}</div>
      <div class=\"text-xs text-slate-500 mt-2\">Copy and store it securely. You won't be able to see it again.</div>
    </div>
    """
    return HTMLResponse(content=html)


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, user: dict = Depends(get_current_user)):
    csrf = _get_csrf_token(request)
    context = {"request": request, "user": user, "page_title": "Account", "csrf_token": csrf}
    resp = templates.TemplateResponse("account/index.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/integrations/partials/grid", response_class=HTMLResponse)
async def integrations_grid_partial(request: Request, user: dict = Depends(get_current_user)):
    """Return integrations grid partial for HTMX polling."""
    db = ensure_db()
    tokens = await get_google_tokens(db, user["user_id"])  # type: ignore
    google_connected = bool(tokens)
    integrations = {
        "gmail": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
        "drive": {"connected": google_connected, "status": ("completed" if google_connected else "disconnected"), "status_label": ("Connected" if google_connected else "Disconnected")},
        "youtube": {"connected": False, "status": "disconnected", "status_label": "Disconnected"},
    }
    csrf = _get_csrf_token(request)
    return templates.TemplateResponse("integrations/partials/grid.html", {"request": request, "integrations": integrations, "csrf_token": csrf})
