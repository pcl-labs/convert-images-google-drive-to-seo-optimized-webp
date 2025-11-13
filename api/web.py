from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates
from starlette.responses import Response
from typing import Optional
import os
import uuid

from .models import OptimizeRequest, JobStatusEnum
from .deps import ensure_services, ensure_db, parse_job_progress, get_current_user
from .auth import verify_jwt_token
from .database import create_job, list_jobs
from workers.consumer import process_optimization_job
import asyncio

router = APIRouter()

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _get_csrf_token(request: Request) -> str:
    token = request.cookies.get("csrf_token")
    if not token:
        token = uuid.uuid4().hex
    return token


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # If already authenticated via JWT cookie, go straight to dashboard
    token = request.cookies.get("access_token")
    if token:
        try:
            verify_jwt_token(token)
            return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        except Exception:
            pass
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("auth/login.html", {"request": request, "csrf_token": csrf})
    if not request.cookies.get("csrf_token"):
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax")
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(get_current_user), page: int = 1):
    db = ensure_db()
    jobs_list, total = await list_jobs(db, user["user_id"], page=page, page_size=20, status=None)
    csrf = _get_csrf_token(request)
    context = {
        "request": request,
        "user": user,
        "jobs": jobs_list,
        "total": total,
        "page": page,
        "csrf_token": csrf,
    }
    resp = templates.TemplateResponse("jobs/dashboard.html", context)
    if not request.cookies.get("csrf_token"):
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax")
    return resp


@router.post("/jobs", response_class=HTMLResponse)
async def create_job_html(
    request: Request,
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
    await create_job(db, job_id, user["user_id"], folder_input, None)

    req_model = OptimizeRequest(drive_folder=folder_input, extensions=None)
    enqueued = await queue.send_job(job_id, user["user_id"], req_model)
    # If no queue configured in local dev, run inline in background so the job progresses
    if not enqueued:
        try:
            # Use defaults similar to API model
            asyncio.create_task(
                process_optimization_job(
                    db=db,
                    job_id=job_id,
                    user_id=user["user_id"],
                    drive_folder=folder_input,
                    extensions=req_model.extensions or [],
                    overwrite=req_model.overwrite,
                    skip_existing=req_model.skip_existing,
                    cleanup_originals=req_model.cleanup_originals,
                    max_retries=req_model.max_retries,
                )
            )
        except Exception:
            # If inline kickoff fails, we still return the refreshed list; job will remain pending/failed
            pass

    jobs_list, total = await list_jobs(db, user["user_id"], page=1, page_size=20, status=None)

    return templates.TemplateResponse(
        "jobs/partials/jobs_list.html",
        {"request": request, "jobs": jobs_list, "total": total, "page": 1},
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
            "status": data.status.value if hasattr(data.status, "value") else str(data.status),
            "error": data.error,
            "drive_folder": data.drive_folder,
            "progress": progress,
        },
    }
    return templates.TemplateResponse("jobs/detail.html", context)
