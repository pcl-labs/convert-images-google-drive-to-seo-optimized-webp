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

from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from starlette.templating import Jinja2Templates
from starlette.responses import Response, StreamingResponse
from typing import Optional, Dict, Any
import os
import uuid
import logging
import json
import secrets
import hmac

from .models import OptimizeDocumentRequest, JobStatusEnum, JobType, GenerateBlogRequest, GenerateBlogOptions
from .deps import ensure_services, ensure_db, parse_job_progress, get_current_user
from .auth import verify_jwt_token
from .database import (
    list_jobs,
    get_job_stats,
    list_google_tokens,
    get_job,
    update_job_status,
    delete_google_tokens,
    create_notification,
    list_notifications,
    mark_notification_seen,
    dismiss_notification,
    get_user_by_id,
    list_documents,
    list_jobs_by_document,
    get_document,
    list_document_versions,
    get_document_version,
    create_document_export,
)
from .auth import create_user_api_key
from .protected import (
    get_job_status as protected_get_job_status,
    start_optimize_job,
    create_drive_document_for_user,
    start_ingest_youtube_job,
    start_ingest_text_job,
    start_generate_blog_job,
)
from .config import settings
from .notifications import notify_job
from .notifications_stream import notifications_stream_response
from .constants import COOKIE_OAUTH_STATE, COOKIE_GOOGLE_OAUTH_STATE
from .utils import normalize_ui_status
from core.constants import GOOGLE_SCOPE_DRIVE, GOOGLE_SCOPE_YOUTUBE, GOOGLE_SCOPE_GMAIL, GOOGLE_INTEGRATION_SCOPES
from .google_oauth import parse_google_scope_list

logger = logging.getLogger(__name__)

# Read-only mapping from job_type to UI kind label
KIND_MAP = {
    "optimize_drive": "Drive",
    "ingest_drive_folder": "Drive",
    "ingest_youtube": "YouTube",
    "ingest_text": "Text",
    "generate_blog": "Blog",
}

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

INTEGRATION_STATUS_LABELS = {
    "completed": "Connected",
    "disconnected": "Disconnected",
    "action_required": "Reconnect required",
}

GOOGLE_SCOPE_LABELS = {
    GOOGLE_SCOPE_DRIVE: "Drive access",
    GOOGLE_SCOPE_YOUTUBE: "YouTube (full access - required for captions)",
    "https://www.googleapis.com/auth/youtube.force-ssl": "YouTube (full access - required for captions)",
    GOOGLE_SCOPE_GMAIL: "Gmail read-only",
}


def _scope_names(scopes: list[str]) -> list[str]:
    return [GOOGLE_SCOPE_LABELS.get(scope, scope) for scope in scopes]


def _build_google_integration_entries(token_rows: Optional[list[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    token_map: Dict[str, Dict[str, Any]] = {}
    if token_rows:
        for row in token_rows:
            key = str(row.get("integration") or "").lower()
            if key:
                token_map[key] = row

    def _entry(
        key: str,
        *,
        hints: Dict[str, str],
    ) -> Dict[str, Any]:
        row = token_map.get(key)
        granted_scopes = parse_google_scope_list(row.get("scopes")) if row else []
        required_scopes = GOOGLE_INTEGRATION_SCOPES.get(key, [])
        missing_scope_ids = [scope for scope in required_scopes if scope not in granted_scopes]
        connected = bool(row) and not missing_scope_ids
        needs_reconnect = bool(row) and bool(missing_scope_ids)
        if connected:
            status = "completed"
            hint = hints["connected"]
        elif needs_reconnect:
            status = "action_required"
            hint = hints["reconnect"]
        else:
            status = "disconnected"
            hint = hints["connect"]
        return {
            "connected": connected,
            "needs_reconnect": needs_reconnect,
            "status": status,
            "status_label": INTEGRATION_STATUS_LABELS.get(status, status.title()),
            "status_hint": hint,
            "missing_scopes": _scope_names(missing_scope_ids),
            "connect_url": f"/auth/google/start?integration={key}&redirect=/dashboard/integrations/{key}",
            "reconnect_url": f"/auth/google/start?integration={key}&redirect=/dashboard/integrations/{key}",
            "connected_at": row.get("created_at") if row else None,
            "granted_scopes": _scope_names(granted_scopes),
            "scopes_raw": granted_scopes,
            "account_connected": bool(row),
            "can_disconnect": bool(row),
        }

    drive_entry = _entry(
        "drive",
        hints={
            "connected": "Drive folders are ready for ingestion.",
            "reconnect": "Reconnect Google to grant Drive access.",
            "connect": "Connect Google Drive to sync folders.",
        },
    )
    youtube_entry = _entry(
        "youtube",
        hints={
            "connected": "YouTube ingestion and metadata fetch are enabled.",
            "reconnect": "Reconnect Google to add the YouTube scope (required for captions API).",
            "connect": "Connect Google to unlock YouTube ingestion.",
        },
    )
    return {
        "drive": drive_entry,
        "youtube": youtube_entry,
    }


def _build_integrations_model(tokens: list[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    google_entries = _build_google_integration_entries(tokens)
    google_entries["gmail"] = {
        "connected": False,
        "needs_reconnect": False,
        "status": "planned",
        "status_label": "Not available",
        "status_hint": "Gmail workflows are on the roadmap.",
    }
    return google_entries

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


def _json_field(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    if value is None:
        return default
    return value


def _form_bool(value: Optional[str]) -> bool:
    if value is None:
        return False
    return str(value).lower() in {"1", "true", "yes", "on"}


def _document_to_view(doc: dict) -> dict:
    metadata = doc.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    elif metadata is None:
        metadata = {}
    frontmatter = _json_field(doc.get("frontmatter"), {})
    latest_generation = metadata.get("latest_generation") if isinstance(metadata, dict) else {}
    source_type = (doc.get("source_type") or "unknown").lower()
    source_label = {
        "drive": "Drive",
        "drive_folder": "Drive",
        "youtube": "YouTube",
        "text": "Text",
    }.get(source_type, source_type.title())
    origin = doc.get("source_ref")
    if source_type == "drive":
        origin = metadata.get("input") or origin
    elif source_type == "youtube":
        origin = metadata.get("url") or origin
    elif source_type == "text":
        origin = metadata.get("title") or "Manual text"
    return {
        "id": doc.get("document_id"),
        "source_type": source_type,
        "source_label": source_label,
        "origin": origin,
        "metadata": metadata,
        "frontmatter": frontmatter,
        "latest_version_id": doc.get("latest_version_id"),
        "content_format": doc.get("content_format"),
        "latest_title": frontmatter.get("title") if isinstance(frontmatter, dict) else None,
        "latest_generation": latest_generation,
        "created_at": doc.get("created_at"),
    }


async def _load_document_views(db, user_id: str, page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    docs, total = await list_documents(db, user_id, page=page, page_size=page_size)
    return [_document_to_view(doc) for doc in docs], total


async def _render_documents_partial(
    request: Request,
    db,
    user: dict,
    flash: Optional[dict] = None,
    status_code: int = status.HTTP_200_OK,
):
    documents, _ = await _load_document_views(db, user["user_id"])
    context = {"request": request, "documents": documents, "flash": flash}
    return templates.TemplateResponse("documents/partials/list.html", context, status_code=status_code)


def _version_summary_row(row: dict) -> dict:
    frontmatter = _json_field(row.get("frontmatter"), {})
    return {
        "version_id": row.get("version_id"),
        "document_id": row.get("document_id"),
        "version": row.get("version"),
        "content_format": row.get("content_format"),
        "frontmatter": frontmatter,
        "created_at": row.get("created_at"),
        "title": (frontmatter or {}).get("title"),
        "slug": (frontmatter or {}).get("slug"),
    }


def _version_detail_row(row: dict) -> dict:
    summary = _version_summary_row(row)
    summary.update(
        {
            "body_mdx": row.get("body_mdx"),
            "body_html": row.get("body_html"),
            "outline": _json_field(row.get("outline"), []),
            "chapters": _json_field(row.get("chapters"), []),
            "sections": _json_field(row.get("sections"), []),
            "assets": _json_field(row.get("assets"), {}),
        }
    )
    return summary


async def _load_latest_version(db, user_id: str, document_id: str) -> Optional[dict]:
    rows = await list_document_versions(db, document_id, user_id, limit=1)
    if not rows:
        return None
    return _version_detail_row(rows[0])


async def _load_version_detail(db, user_id: str, document_id: str, version_id: str) -> Optional[dict]:
    row = await get_document_version(db, document_id, version_id, user_id)
    if not row:
        return None
    return _version_detail_row(row)


def _render_flash(request: Request, message: str, kind: str = "info", status_code: int = status.HTTP_200_OK) -> HTMLResponse:
    context = {"request": request, "flash_kind": kind, "flash_message": message}
    return templates.TemplateResponse("documents/partials/flash.html", context, status_code=status_code)


def _render_version_partial(request: Request, document: dict, version: Optional[dict], csrf_token: str) -> HTMLResponse:
    context = {
        "request": request,
        "document": document,
        "version": version,
        "csrf_token": csrf_token,
    }
    return templates.TemplateResponse("documents/partials/version_viewer.html", context)


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


@router.get("/styleguide", response_class=HTMLResponse)
async def styleguide(request: Request):
    """Design system styleguide page."""
    return templates.TemplateResponse("styleguide.html", {"request": request, "title": "Design System Styleguide"})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, page: int = 1, user: dict = Depends(get_current_user)):
    
    db = ensure_db()
    jobs_list, total = await list_jobs(db, user["user_id"], page=page, page_size=10, status=None)
    stats = await get_job_stats(db, user["user_id"])  # { total, completed, failed, pending, processing }
    csrf = _get_csrf_token(request)
    # Google connection status for dashboard badge
    google_tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    token_map = {str(row.get("integration")).lower(): row for row in (google_tokens or []) if row.get("integration")}
    drive_connected = "drive" in token_map
    youtube_connected = "youtube" in token_map

    # Transform jobs for dashboard table
    def to_view(j):
        status = j.get("status", "queued")
        jt = (j.get("job_type") or "optimize_drive")
        return {
            "id": j.get("job_id"),
            "document_id": j.get("document_id"),
            "kind": KIND_MAP.get(jt, jt.replace("_", " ").title()),
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
        "drive_connected": drive_connected,
        "youtube_connected": youtube_connected,
    }
    resp = templates.TemplateResponse("dashboard/index.html", context)
    if not request.cookies.get("csrf_token"):
        # Set secure cookies only in production or when the request is over HTTPS
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/dashboard/documents", response_class=HTMLResponse)
async def documents_page(request: Request, page: int = 1, user: dict = Depends(get_current_user)):
    db = ensure_db()
    csrf = _get_csrf_token(request)
    documents, total = await _load_document_views(db, user["user_id"], page=page, page_size=20)
    google_tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    token_map = {str(row.get("integration")).lower(): row for row in (google_tokens or []) if row.get("integration")}
    drive_connected = "drive" in token_map
    youtube_connected = "youtube" in token_map
    context = {
        "request": request,
        "user": user,
        "documents": documents,
        "total_documents": total,
        "csrf_token": csrf,
        "drive_connected": drive_connected,
        "youtube_connected": youtube_connected,
        "page_title": "Documents",
        "flash": None,
    }
    resp = templates.TemplateResponse("documents/index.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = _is_secure_request(request)
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/dashboard/documents/{document_id}", response_class=HTMLResponse)
async def document_detail_page(document_id: str, request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    doc = await get_document(db, document_id, user_id=user["user_id"])
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    doc_view = _document_to_view(doc)
    metadata_items = list((doc_view.get("metadata") or {}).items())
    frontmatter_items = list((doc_view.get("frontmatter") or {}).items())
    versions_raw = await list_document_versions(db, document_id, user["user_id"], limit=20)
    version_summaries = [_version_summary_row(row) for row in versions_raw]
    latest_version = _version_detail_row(versions_raw[0]) if versions_raw else None
    jobs = await list_jobs_by_document(db, user["user_id"], document_id, limit=25)
    csrf = _get_csrf_token(request)

    def to_job_view(job: dict) -> dict:
        status = job.get("status", "queued")
        job_type = (job.get("job_type") or "optimize_drive")
        return {
            "id": job.get("job_id"),
            "kind": KIND_MAP.get(job_type, job_type.replace("_", " ").title()),
            "status": status,
            "status_label": _status_label(status),
            "created_at": job.get("created_at"),
            "document_id": job.get("document_id"),
        }

    context = {
        "request": request,
        "user": user,
        "document": doc_view,
        "metadata_items": metadata_items,
        "frontmatter_items": frontmatter_items,
        "jobs": [to_job_view(j) for j in jobs],
        "versions": version_summaries,
        "latest_version": latest_version,
        "csrf_token": csrf,
        "page_title": f"Document {document_id}",
    }
    resp = templates.TemplateResponse("documents/detail.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = _is_secure_request(request)
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/dashboard/documents/{document_id}/versions/{version_id}", response_class=HTMLResponse)
async def document_version_partial(document_id: str, version_id: str, request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    doc = await get_document(db, document_id, user_id=user["user_id"])
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    version = await _load_version_detail(db, user["user_id"], document_id, version_id)
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    doc_view = _document_to_view(doc)
    csrf = _get_csrf_token(request)
    return _render_version_partial(request, doc_view, version, csrf)


@router.get("/dashboard/documents/{document_id}/versions/{version_id}/download")
async def download_document_version(document_id: str, version_id: str, format: str = "mdx", user: dict = Depends(get_current_user)):
    db = ensure_db()
    row = await get_document_version(db, document_id, version_id, user["user_id"])
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    version = _version_detail_row(row)
    fmt = format.lower()
    if fmt == "html":
        body = version.get("body_html")
        media_type = "text/html"
        extension = "html"
    else:
        body = version.get("body_mdx")
        media_type = "text/plain"
        extension = "mdx"
    if not body:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{fmt.upper()} body unavailable for this version")
    filename = f"{document_id}-{version_id}.{extension}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return PlainTextResponse(body, media_type=media_type, headers=headers)


@router.post("/dashboard/documents/{document_id}/generate", response_class=HTMLResponse)
async def dashboard_generate_blog(
    document_id: str,
    request: Request,
    tone: str = Form("informative"),
    max_sections: int = Form(5),
    target_chapters: int = Form(4),
    include_images: Optional[str] = Form("on"),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    cookie_token = request.cookies.get("csrf_token")
    if not secrets.compare_digest(str(cookie_token or ""), str(csrf_token or "")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    try:
        options = GenerateBlogOptions(
            tone=tone.strip() or "informative",
            max_sections=max_sections,
            target_chapters=target_chapters,
            include_images=_form_bool(include_images),
        )
    except Exception as exc:
        return _render_flash(request, f"Invalid options: {exc}", "error", status.HTTP_400_BAD_REQUEST)
    req_model = GenerateBlogRequest(document_id=document_id, options=options)
    db, queue = ensure_services()
    try:
        await start_generate_blog_job(db, queue, user["user_id"], req_model)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Failed to queue job"
        return _render_flash(request, detail, "error", exc.status_code)
    return _render_flash(request, "Blog generation job queued", "success")


@router.post("/dashboard/documents/{document_id}/sections/{section_index}/regenerate", response_class=HTMLResponse)
async def dashboard_regenerate_section(
    document_id: str,
    section_index: int,
    request: Request,
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    cookie_token = request.cookies.get("csrf_token")
    if not secrets.compare_digest(str(cookie_token or ""), str(csrf_token or "")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    if section_index < 0:
        return _render_flash(request, "Invalid section index", "error", status.HTTP_400_BAD_REQUEST)
    db, queue = ensure_services()
    try:
        options = GenerateBlogOptions(section_index=section_index)
    except Exception as exc:
        return _render_flash(
            request,
            f"Invalid options: {exc}",
            "error",
            status.HTTP_400_BAD_REQUEST,
        )
    req_model = GenerateBlogRequest(document_id=document_id, options=options)
    try:
        await start_generate_blog_job(db, queue, user["user_id"], req_model)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Failed to queue job"
        return _render_flash(request, detail, "error", exc.status_code)
    return _render_flash(request, f"Regeneration queued for section {section_index + 1}", "success")


@router.post("/dashboard/documents/{document_id}/exports/{target}", response_class=HTMLResponse)
async def dashboard_document_export(
    document_id: str,
    target: str,
    request: Request,
    version_id: Optional[str] = Form(None),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    cookie_token = request.cookies.get("csrf_token")
    if not secrets.compare_digest(str(cookie_token or ""), str(csrf_token or "")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    normalized = target.lower()
    allowed = {"google_docs", "zapier", "wordpress"}
    if normalized not in allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown export target")
    db = ensure_db()
    doc = await get_document(db, document_id, user_id=user["user_id"])
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    resolved_version_id = version_id or doc.get("latest_version_id")
    if not resolved_version_id:
        return _render_flash(request, "No version available to export", "error", status.HTTP_400_BAD_REQUEST)
    version = await get_document_version(db, document_id, resolved_version_id, user["user_id"])
    if not version:
        return _render_flash(request, "Version not found", "error", status.HTTP_404_NOT_FOUND)
    row = await create_document_export(db, document_id, resolved_version_id, user["user_id"], normalized, None)
    label = normalized.replace("_", " ").title()
    message = f"{label} export queued (version {row.get('version_id')})"
    return _render_flash(request, message, "success")


@router.post("/dashboard/jobs", response_class=HTMLResponse)
async def create_job_html(
    request: Request,
    document_id: str = Form(...),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or cookie_token != csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    db = ensure_db()
    _, queue = ensure_services()

    doc_id = (document_id or "").strip()
    if not doc_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Document ID required")

    req_model = OptimizeDocumentRequest(document_id=doc_id)
    try:
        await start_optimize_job(db, queue, user["user_id"], doc_id, req_model)
    except HTTPException as exc:
        raise exc

    jobs_list, total = await list_jobs(db, user["user_id"], page=1, page_size=10, status=None)

    # Transform for partial
    def to_view(j):
        status = j.get("status", "queued")
        return {
            "id": j.get("job_id"),
            "document_id": j.get("document_id"),
            "kind": "Drive",
            "status": status,
            "status_label": _status_label(status),
            "created_at": j.get("created_at"),
        }

    return templates.TemplateResponse(
        "jobs/partials/jobs_list.html",
        {"request": request, "jobs": [to_view(j) for j in jobs_list]},
    )


@router.post("/dashboard/documents/drive", response_class=HTMLResponse)
async def create_drive_document_form(
    request: Request,
    drive_source: str = Form(...),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or cookie_token != csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    source = (drive_source or "").strip()
    if not source:
        flash = {"status": "error", "message": "Drive folder URL or ID is required"}
        return await _render_documents_partial(request, db, user, flash, status_code=status.HTTP_400_BAD_REQUEST)
    try:
        doc = await create_drive_document_for_user(db, user["user_id"], source)
        flash = {"status": "success", "message": f"Registered document {doc.document_id}"}
        return await _render_documents_partial(request, db, user, flash)
    except HTTPException as exc:
        flash = {"status": "error", "message": exc.detail or "Failed to register Drive folder"}
        return await _render_documents_partial(request, db, user, flash, status_code=exc.status_code)
    except Exception:
        logger.exception("Failed to create Drive document via UI")
        flash = {"status": "error", "message": "Unexpected error while registering Drive folder"}
        return await _render_documents_partial(request, db, user, flash, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("/dashboard/documents/youtube", response_class=HTMLResponse)
async def create_youtube_document_form(
    request: Request,
    youtube_url: str = Form(...),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or cookie_token != csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    url = (youtube_url or "").strip()
    if not url:
        flash = {"status": "error", "message": "YouTube URL is required"}
        return await _render_documents_partial(request, db, user, flash, status_code=status.HTTP_400_BAD_REQUEST)
    _, queue = ensure_services()
    try:
        job = await start_ingest_youtube_job(db, queue, user["user_id"], url)
        flash = {
            "status": "success",
            "message": f"YouTube ingest job {job.job_id} queued (doc {job.document_id})",
        }
        return await _render_documents_partial(request, db, user, flash)
    except HTTPException as exc:
        flash = {"status": "error", "message": exc.detail or "Failed to ingest YouTube video"}
        return await _render_documents_partial(request, db, user, flash, status_code=exc.status_code)
    except Exception:
        logger.exception("Failed to queue YouTube ingest from UI")
        flash = {"status": "error", "message": "Unexpected error while ingesting video"}
        return await _render_documents_partial(request, db, user, flash, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("/dashboard/documents/text", response_class=HTMLResponse)
async def create_text_document_form(
    request: Request,
    text_body: str = Form(...),
    title: Optional[str] = Form(None),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or cookie_token != csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    _, queue = ensure_services()
    body = (text_body or "").strip()
    if not body:
        flash = {"status": "error", "message": "Text content is required"}
        return await _render_documents_partial(request, db, user, flash, status_code=status.HTTP_400_BAD_REQUEST)
    try:
        job = await start_ingest_text_job(db, queue, user["user_id"], body, title)
        flash = {
            "status": "success",
            "message": f"Text ingest job {job.job_id} queued (doc {job.document_id})",
        }
        return await _render_documents_partial(request, db, user, flash)
    except HTTPException as exc:
        flash = {"status": "error", "message": exc.detail or "Failed to ingest text"}
        return await _render_documents_partial(request, db, user, flash, status_code=exc.status_code)
    except Exception:
        logger.exception("Failed to queue text ingest from UI")
        flash = {"status": "error", "message": "Unexpected error while ingesting text"}
        return await _render_documents_partial(request, db, user, flash, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
            "document_id": data.document_id,
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
        jt = (j.get("job_type") or "optimize_drive")
        return {
            "id": j.get("job_id"),
            "document_id": j.get("document_id"),
            "kind": KIND_MAP.get(jt, jt.replace("_", " ").title()),
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
    tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    integrations = _build_integrations_model(tokens)
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
    await delete_google_tokens(db, user["user_id"], integration="drive")  # type: ignore
    return RedirectResponse(url="/dashboard/integrations", status_code=status.HTTP_302_FOUND)


@router.get("/dashboard/integrations/partials/grid", response_class=HTMLResponse)
async def integrations_grid_partial(request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    integrations = _build_integrations_model(tokens)
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
    tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    integrations = _build_integrations_model(tokens)
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
    job_type = job.get("job_type")
    if job_type != JobType.OPTIMIZE_DRIVE.value:
        return Response(
            content="Retry is only supported for Drive optimization jobs right now.",
            status_code=status.HTTP_400_BAD_REQUEST,
            media_type="text/plain"
        )
    document_id = job.get("document_id")
    if not document_id:
        return Response(
            content="Job is missing document reference and cannot be retried.",
            status_code=status.HTTP_400_BAD_REQUEST,
            media_type="text/plain"
        )
    payload = {
        "job_id": job_id,
        "user_id": user["user_id"],
        "job_type": job_type,
        "document_id": document_id,
        "extensions": extensions_list,
        "overwrite": False,
        "skip_existing": True,
        "cleanup_originals": False,
        "max_retries": 3,
    }
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(
        queue, job_id, user["user_id"], payload, allow_inline_fallback=False
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
async def integration_disconnect(service: str, request: Request, csrf_token: str = Form(...), user: dict = Depends(get_current_user)):
    cookie_token = request.cookies.get("csrf_token")
    if cookie_token is None or csrf_token is None or not hmac.compare_digest(str(cookie_token), str(csrf_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    integration_key = service.lower()
    if integration_key not in {"youtube", "gmail"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if integration_key == "gmail":
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Gmail disconnect not implemented yet")
    db = ensure_db()
    await delete_google_tokens(db, user["user_id"], integration=integration_key)  # type: ignore
    return RedirectResponse(url="/dashboard/integrations", status_code=status.HTTP_302_FOUND)


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
