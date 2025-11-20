from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from starlette.templating import Jinja2Templates
from starlette.responses import Response, StreamingResponse
from typing import Optional, Dict, Any, List
import os
import asyncio
import uuid
import logging
import json
import secrets
import hmac
# Jinja2 imports removed - using Jinja2Templates directly

from .models import (
    OptimizeDocumentRequest,
    JobStatusEnum,
    JobType,
    GenerateBlogRequest,
    GenerateBlogOptions,
)
from .deps import ensure_services, ensure_db, parse_job_progress, get_current_user
from .auth import verify_jwt_token
from .database import (
    list_jobs,
    get_job_stats,
    list_google_tokens,
    get_job,
    update_job_status,
    delete_google_tokens,
    delete_user_account,
    get_user_preferences,
    update_user_preferences,
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
    update_document,
    get_drive_workspace,
    upsert_drive_workspace,
    list_pipeline_events,
    latest_job_by_type,
    delete_user_session,
)
from .auth import create_user_api_key
from .protected import (
    get_job_status as protected_get_job_status,
    start_optimize_job,
    create_drive_document_for_user,
    start_ingest_youtube_job,
    start_ingest_text_job,
    start_generate_blog_job,
    enqueue_job_with_guard,
)
from .config import settings
from .utils import is_secure_request
from .notifications import notify_job, notify_activity
from .notifications_stream import notifications_stream_response
from .pipeline_stream import pipeline_stream_response
from .flash import add_flash
from .constants import COOKIE_OAUTH_STATE, COOKIE_GOOGLE_OAUTH_STATE
from .utils import normalize_ui_status
from core.constants import GOOGLE_SCOPE_DRIVE, GOOGLE_SCOPE_YOUTUBE, GOOGLE_SCOPE_GMAIL, GOOGLE_INTEGRATION_SCOPES
from .google_oauth import parse_google_scope_list, build_docs_service_for_user, build_drive_service_for_user
from core.google_async import execute_google_request
from .drive_workspace import ensure_drive_workspace
from .drive_docs import sync_drive_doc_for_document
from .ai_preferences import (
    get_ai_model_choices,
    normalize_ai_preferences,
    set_ai_preferences,
)

CONTENT_SCHEMA_CHOICES = [
    ("https://schema.org/BlogPosting", "Blog post"),
    ("https://schema.org/FAQPage", "FAQ Page"),
    ("https://schema.org/HowTo", "How-To Guide"),
    ("https://schema.org/Recipe", "Recipe"),
]


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


logger = logging.getLogger(__name__)

# Read-only mapping from job_type to UI kind label
KIND_MAP = {
    "optimize_drive": "Drive",
    "ingest_drive_folder": "Drive",
    "ingest_drive": "Drive",
    "drive_change_poll": "Drive",
    "drive_watch_renewal": "Drive",
    "ingest_youtube": "YouTube",
    "ingest_text": "Text",
    "generate_blog": "Blog",
}


def _validate_csrf(request: Request, form_token: Optional[str]) -> None:
    """Validate CSRF token from cookie against form token.
    
    Raises HTTPException with 403 status if either token is missing or tokens don't match.
    Uses timing-safe comparison via hmac.compare_digest.
    """
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or not form_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    if not hmac.compare_digest(str(cookie_token), str(form_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


router = APIRouter()

# Templates are packaged in src/workers/templates/
# Use simple filesystem-based Jinja2Templates
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # src/workers
TEMPLATES_DIR = BASE_DIR / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

base_url_value = (settings.base_url or "").strip()
templates.env.globals["base_url"] = base_url_value.rstrip("/") if base_url_value else ""

# Explicitly register url_for as a global function for template use
# This ensures url_for is available even if Starlette's automatic injection
# doesn't work reliably in the Cloudflare Worker runtime
from jinja2 import pass_context

@pass_context
def _url_for(context, name: str, **path_params: str) -> str:
    """Jinja helper for generating URLs using FastAPI's url_for.
    
    Templates call this as: {{ url_for('static', path='css/app.css') }}
    which maps to request.url_for('static', path='css/app.css')
    
    The 'request' object is extracted from the template context automatically.
    """
    request = context.get('request')
    if request is None:
        logger.error("url_for called without 'request' in template context")
        raise RuntimeError("'request' not found in template context. Ensure 'request' is passed to TemplateResponse.")
    
    try:
        return request.url_for(name, **path_params)
    except Exception as exc:
        # Log the error for debugging
        logger.error(
            "url_for failed: name=%s, path_params=%s, error=%s, error_type=%s",
            name,
            path_params,
            str(exc),
            type(exc).__name__,
            exc_info=True,
        )
        # For static files, fallback to a simple path construction
        if name == "static" and "path" in path_params:
            fallback_path = f"/static/{path_params['path']}"
            logger.warning("Using fallback path for static file: %s", fallback_path)
            return fallback_path
        # Re-raise for other cases
        raise

templates.env.globals["url_for"] = _url_for

# Register Jinja filter once (after templates is initialized)
templates.env.filters["status_label"] = _status_label

# Verify critical templates can be loaded (diagnostic check)
# This helps catch template packaging/bundling issues early
def _verify_templates_available():
    """Verify that critical templates can be loaded.
    
    This is a lightweight sanity check to catch template packaging issues.
    We don't fail startup if this fails, but we log clearly so issues are visible.
    """
    critical_templates = ["home.html", "base_public.html"]
    missing = []
    for template_name in critical_templates:
        try:
            templates.get_template(template_name)
        except Exception as exc:
            missing.append(f"{template_name}: {exc}")
            logger.warning("Template %s not available: %s", template_name, exc)
    
    if missing:
        logger.error(
            "Template loading check failed. Missing or inaccessible templates: %s. "
            "This may indicate a packaging/bundling issue in the Worker environment. "
            "TEMPLATES_DIR=%s",
            ", ".join(missing),
            TEMPLATES_DIR
        )
    else:
        logger.debug("Template loading check passed: all critical templates available")

# Run check once at module import (after templates is initialized)
_verify_templates_available()

# Track background tasks to avoid premature garbage collection
BACKGROUND_TASKS: set[asyncio.Task] = set()

def _track_task(task: asyncio.Task) -> None:
    BACKGROUND_TASKS.add(task)
    def _on_done(t: asyncio.Task) -> None:
        BACKGROUND_TASKS.discard(t)
        try:
            exc = t.exception()
            if exc is not None:
                logger.exception("background_task_failed", extra={"error": str(exc)})
        except asyncio.CancelledError:
            logger.info("background_task_cancelled")
        except Exception as cb_exc:
            logger.warning("background_task_done_callback_error", exc_info=True, extra={"error": str(cb_exc)})
    task.add_done_callback(_on_done)

# Centralized service metadata used across integrations views
SERVICES_META = {
    "github": {
        "key": "github",
        "name": "GitHub",
        "capability": "Authentication",
        "description": "Sign in with GitHub to manage developer workflows.",
        "category": "Developer tools",
        "developer": "GitHub",
        "website": "https://github.com/",
        "privacy": "https://docs.github.com/en/site-policy/privacy-policies/github-privacy-statement",
        "created_at": None,
    },
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
        "description": "Create a Quill workspace folder and sync Google Docs automatically.",
        "long_description": "Quill provisions a Drive workspace for you the moment you connect Google Drive. Each document gets a dedicated folder plus its Google Doc so you can draft directly in Drive while Quill keeps track of versions and publishing status.",
        "value_props": [
            {
                "title": "Workspace automation",
                "body": "We create the Quill root folder and per-document subfolders so every generated document has a predictable home in Drive.",
            },
            {
                "title": "Single source of truth",
                "body": "The Google Doc inside each folder stays in sync with Quill, so draft vs. published state is tracked in-app instead of by juggling folders.",
            },
            {
                "title": "Media-ready folders",
                "body": "Each document folder includes a Media directory for screenshots or assets that need to ship with the blog post.",
            },
        ],
        "synced_content": [
            {
                "label": "Workspace",
                "path": "My Drive / Quill",
                "description": "Created automatically with Drive connection. Houses all Quill-managed document folders.",
            },
            {
                "label": "Document folders",
                "path": "My Drive / Quill / {Document}",
                "description": "Each Quill document gets its own folder with the linked Google Doc inside.",
            },
            {
                "label": "Media",
                "path": "My Drive / Quill / {Document} / Media",
                "description": "Optional asset folder for screenshots or supporting imagery referenced in the post.",
            },
        ],
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
        "description": "Pull channel content into Quill so AI can turn videos into ready-to-publish blogs.",
        "long_description": "Connect YouTube to fetch transcripts, titles, and thumbnails for your latest videos. Quill ingests that context into Documents so AI can outline, draft, and optimize publish-ready blog posts without manual copy/paste.",
        "value_props": [
            {
                "title": "Channel-aware ingestion",
                "body": "Quill fetches public and unlisted videos (with granted scopes) to seed new Documents with transcripts and chapter markers.",
            },
            {
                "title": "AI blog generation",
                "body": "Use the fetched metadata plus Quill's blog generator to convert each video into long-form content in a few clicks.",
            },
            {
                "title": "Auto-linked documents",
                "body": "Every ingested video produces a Document entry so you can track drafts, exports, and status per video.",
            },
        ],
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
    "https://www.googleapis.com/auth/documents": "Docs editor",
    GOOGLE_SCOPE_YOUTUBE: "YouTube (full access - required for captions)",
    "https://www.googleapis.com/auth/youtube.force-ssl": "YouTube (full access - required for captions)",
    GOOGLE_SCOPE_GMAIL: "Gmail read-only",
}


ALLOWED_INTEGRATION_KEYS = {"drive", "youtube"}


def _github_info_for(user: dict, stored_user: dict | None) -> dict:
    return {
        "github_id": user.get("github_id") or ((stored_user or {}).get("github_id")),
        "email": user.get("email") or ((stored_user or {}).get("email")),
        "created_at": (stored_user or {}).get("created_at"),
    }


def _services_meta_filtered() -> dict:
    # Only show Drive and YouTube in Integrations UI (auth providers live on Account page)
    return {k: v for k, v in SERVICES_META.items() if k in ALLOWED_INTEGRATION_KEYS}


def _filter_integrations_map(integrations: dict) -> dict:
    return {k: v for k, v in integrations.items() if k in ALLOWED_INTEGRATION_KEYS}


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


def _build_integrations_model(
    tokens: list[Dict[str, Any]],
    *,
    github_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    google_entries = _build_google_integration_entries(tokens)
    google_entries["gmail"] = {
        "connected": False,
        "needs_reconnect": False,
        "status": "planned",
        "status_label": "Not available",
        "status_hint": "Gmail workflows are on the roadmap.",
    }
    integrations = dict(google_entries)

    info = github_info or {}
    github_id = info.get("github_id")
    email = info.get("email")
    connected_at = info.get("created_at")
    github_connected = bool(github_id)
    status = "completed" if github_connected else "disconnected"
    status_label = "Connected" if github_connected else "Disconnected"
    if github_connected:
        if github_id and email:
            hint = f"Signed in with GitHub user {github_id} ({email})."
        elif github_id:
            hint = f"Signed in with GitHub user {github_id}."
        elif email:
            hint = f"Signed in with GitHub account {email}."
        else:
            hint = "GitHub account linked for authentication."
    else:
        hint = "Connect GitHub to enable developer authentication."

    integrations["github"] = {
        "key": "github",
        "connected": github_connected,
        "needs_reconnect": False,
        "status": status,
        "status_label": status_label,
        "status_hint": hint,
        "connect_url": "/auth/github/start",
        "reconnect_url": "/auth/github/start",
        "granted_scopes": ["user:email"] if github_connected else [],
        "connected_at": connected_at,
        "can_disconnect": False,
    }

    return integrations

def _get_csrf_token(request: Request) -> str:
    token = request.cookies.get("csrf_token")
    if not token:
        token = uuid.uuid4().hex
    return token


# Removed _is_secure_request - now using shared is_secure_request from utils


def _render_auth_page(request: Request, view_mode: str) -> Response:
    """Render shared login/signup template with CSRF setup."""
    if getattr(request.state, "user", None):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    csrf = _get_csrf_token(request)
    context = {"request": request, "csrf_token": csrf, "view_mode": view_mode}
    resp = templates.TemplateResponse("auth/login.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = is_secure_request(request, settings)
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


async def _render_account_page(
    request: Request,
    user: dict,
    *,
    status_code: int = status.HTTP_200_OK,
    delete_error: Optional[str] = None,
    delete_value: str = "",
    ai_message: Optional[str] = None,
    ai_error: Optional[str] = None,
) -> Response:
    """Render the Account page with optional deletion error context."""

    csrf = _get_csrf_token(request)
    db = ensure_db()
    stored = None
    try:
        stored = await get_user_by_id(db, user.get("user_id"))  # type: ignore[arg-type]
    except Exception:
        stored = None

    display_user = {
        "user_id": user.get("user_id"),
        "github_id": user.get("github_id") or ((stored or {}).get("github_id")),
        "google_id": user.get("google_id") or ((stored or {}).get("google_id")),
        "email": user.get("email") or ((stored or {}).get("email")),
        "created_at": (stored or {}).get("created_at"),
    }
    preferences_blob = _json_field((stored or {}).get("preferences"), {})
    ai_preferences = normalize_ai_preferences(preferences_blob.get("ai"))

    context = {
        "request": request,
        "user": display_user,
        "page_title": "Account",
        "csrf_token": csrf,
        "delete_error": delete_error,
        "delete_value": delete_value or "",
        "preferences": preferences_blob,
        "ai_preferences": ai_preferences,
        "ai_model_choices": get_ai_model_choices(),
        "ai_message": ai_message,
        "ai_error": ai_error,
    }

    resp = templates.TemplateResponse("account/index.html", context, status_code=status_code)
    if not request.cookies.get("csrf_token"):
        is_secure = is_secure_request(request, settings)
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
    drive_meta = metadata.get("drive") if isinstance(metadata, dict) else None
    if not isinstance(drive_meta, dict):
        drive_meta = {}
    if doc.get("drive_file_id"):
        drive_meta.setdefault("file_id", doc.get("drive_file_id"))
    if doc.get("drive_revision_id"):
        drive_meta.setdefault("revision_id", doc.get("drive_revision_id"))
    if metadata is not None:
        metadata["drive"] = drive_meta
    frontmatter = _json_field(doc.get("frontmatter"), {})
    latest_generation = metadata.get("latest_generation") if isinstance(metadata, dict) else {}
    latest_outline = metadata.get("latest_outline") if isinstance(metadata, dict) else []
    content_plan = metadata.get("content_plan") if isinstance(metadata, dict) else None
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
        "drive_file_id": doc.get("drive_file_id"),
        "drive_revision_id": doc.get("drive_revision_id"),
        "latest_title": frontmatter.get("title") if isinstance(frontmatter, dict) else None,
        "latest_generation": latest_generation,
        "latest_outline": latest_outline,
        "content_plan": content_plan,
        "created_at": doc.get("created_at"),
        "raw_text": doc.get("raw_text"),
        "drive_sync_status": metadata.get("drive_sync_status") if isinstance(metadata, dict) else None,
    }


async def _load_document_views(db, user_id: str, page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    docs, total = await list_documents(db, user_id, page=page, page_size=page_size)
    return [_document_to_view(doc) for doc in docs], total


async def _provision_workspace_background(db, user_id: str) -> None:
    try:
        await ensure_drive_workspace(db, user_id)
    except Exception as exc:
        logger.exception(
            "drive_workspace_provision_bg_failed",
            extra={"user_id": user_id, "error": str(exc)},
        )


def _drive_document_entry(doc: dict) -> Optional[dict]:
    if not (doc.get("source_type") or "").startswith("drive"):
        return None
    meta = _json_field(doc.get("metadata"), {})
    drive_meta = meta.get("drive") if isinstance(meta, dict) else {}
    if not isinstance(drive_meta, dict) or not drive_meta:
        return None
    frontmatter = _json_field(doc.get("frontmatter"), {})
    folder = drive_meta.get("folder") or {}
    media = drive_meta.get("media") or {}
    title = (
        (frontmatter.get("title") if isinstance(frontmatter, dict) else None)
        or folder.get("name")
        or doc.get("latest_title")
        or doc.get("id")
    )
    return {
        "document_id": doc.get("id"),
        "title": title,
        "frontmatter": frontmatter if isinstance(frontmatter, dict) else {},
        "source_label": doc.get("source_label"),
        "drive": {
            "folder_link": folder.get("webViewLink"),
            "folder_id": folder.get("id") or drive_meta.get("folder_id"),
            "doc_link": drive_meta.get("web_view_link"),
            "media_link": media.get("webViewLink"),
            "media_folder_id": media.get("id") or drive_meta.get("media_folder_id"),
            "file_id": drive_meta.get("file_id"),
            "revision_id": drive_meta.get("revision_id"),
        },
        "stage": drive_meta.get("stage"),
    }


def _drive_sync_overview(
    documents: list[dict],
    drive_connected: bool,
    workspace: Optional[dict] = None,
) -> dict:
    drive_entries = [entry for doc in documents for entry in [_drive_document_entry(doc)] if entry]
    latest_created = next(
        (
            doc.get("created_at")
            for doc in documents
            if doc.get("created_at") and (doc.get("source_type") or "").startswith("drive")
        ),
        None,
    )
    overview: dict[str, Any] = {
        "connected": drive_connected,
        "status_label": "Connected" if drive_connected else "Not connected",
        "document_count": len(drive_entries),
        "last_synced_at": latest_created or ("Not synced yet" if drive_connected else None),
        "detail_url": "/dashboard/integrations/drive",
        "documents": drive_entries[:12],
    }
    if drive_entries:
        latest_doc = drive_entries[0]
        drive_meta = latest_doc.get("drive") or {}
        overview["linked_file"] = {
            "title": latest_doc.get("title") or latest_doc.get("document_id"),
            "file_id": drive_meta.get("file_id"),
            "document_id": latest_doc.get("document_id"),
            "link": drive_meta.get("doc_link") or drive_meta.get("draft_link"),
        }
    if workspace:
        workspace_meta = _json_field(workspace.get("metadata"), {})
        folders = []
        def _folder_entry(label: str, key: str, fallback_id: Optional[str]) -> Optional[dict]:
            data = (workspace_meta or {}).get(key) if isinstance(workspace_meta, dict) else {}
            if not isinstance(data, dict):
                data = {}
            folder_id = data.get("id") or fallback_id
            if not folder_id:
                return None
            return {
                "label": label,
                "name": data.get("name") or key.title(),
                "id": folder_id,
                "link": data.get("webViewLink"),
            }
        root_entry = _folder_entry("Workspace", "root", workspace.get("root_folder_id") if workspace else None)
        root_id = root_entry.get("id") if root_entry else None
        if root_entry:
            folders.append(root_entry)
        drafts_entry = _folder_entry("Legacy Drafts", "drafts", workspace.get("drafts_folder_id") if workspace else None)
        if drafts_entry and drafts_entry["id"] != root_id:
            folders.append(drafts_entry)
        published_entry = _folder_entry("Legacy Published", "published", workspace.get("published_folder_id") if workspace else None)
        existing_ids = {entry["id"] for entry in folders}
        if published_entry and published_entry["id"] not in existing_ids:
            folders.append(published_entry)
        overview["folders"] = folders
    return overview


async def _export_version_to_drive(db, user_id: str, document: dict, version: dict) -> dict:
    """Push a document version into Drive using per-request HTTP clients for thread-safety."""
    docs_service = await build_docs_service_for_user(db, user_id)
    drive_service = await build_drive_service_for_user(db, user_id)
    metadata = document.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    drive_block = metadata.get("drive") if isinstance(metadata, dict) else {}
    if not isinstance(drive_block, dict):
        drive_block = {}
    drive_file_id = document.get("drive_file_id") or drive_block.get("file_id")
    frontmatter = _json_field(version.get("frontmatter"), {})
    title = frontmatter.get("title")
    if not drive_file_id:
        created = await execute_google_request(
            docs_service.documents().create(body={"title": title or f"Quill Draft {document.get('document_id')}"})
        )
        if not created:
            logger.error(
                "drive_create_doc_failed",
                extra={"document_id": document.get("document_id"), "user_id": user_id},
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to create Google Doc")
        drive_file_id = created.get("documentId")
        if not drive_file_id:
            logger.error(
                "drive_create_doc_missing_id",
                extra={"document_id": document.get("document_id"), "user_id": user_id, "created": created},
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Google Doc creation returned no documentId")
    text_body = version.get("body_mdx") or version.get("body_html") or ""
    try:
        current_doc = await execute_google_request(docs_service.documents().get(documentId=drive_file_id))
        body_content = (current_doc.get("body", {}) or {}).get("content", []) or []
        last_endIndex = body_content[-1].get("endIndex", max(2, len(text_body) + 1)) if body_content else 2
        end_index = max(2, (last_endIndex - 1))
    except Exception as exc:
        logger.warning(
            "Failed to get current document for end_index calculation, using fallback",
            exc_info=True,
            extra={"drive_file_id": drive_file_id, "error": str(exc)}
        )
        end_index = 2
    requests = [
        {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index}}},
        {"insertText": {"location": {"index": 1}, "text": text_body}},
    ]
    await execute_google_request(
        docs_service.documents().batchUpdate(documentId=drive_file_id, body={"requests": requests})
    )
    drive_meta = await execute_google_request(
        drive_service.files().get(fileId=drive_file_id, fields='id, headRevisionId, webViewLink')
    )
    drive_block.update(
        {
            "file_id": drive_file_id,
            "revision_id": drive_meta.get("headRevisionId"),
            "web_view_link": drive_meta.get("webViewLink"),
        }
    )
    metadata["drive"] = drive_block
    try:
        await update_document(
            db,
            document.get("document_id"),
            {
                "metadata": metadata,
                "drive_file_id": drive_file_id,
                "drive_revision_id": drive_meta.get("headRevisionId"),
            },
        )
    except Exception as exc:
        logger.error(
            "Failed to update document after Drive export",
            exc_info=True,
            extra={"document_id": document.get("document_id"), "drive_file_id": drive_file_id},
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to record Drive export")
    return {"file_id": drive_file_id, "revision_id": drive_meta.get("headRevisionId")}


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


async def _render_ingest_response(
    request: Request,
    db,
    user: dict,
    flash: dict,
    status_code: int = status.HTTP_200_OK,
):
    """Render either the documents table or a lightweight flash card depending on HX target."""
    target = (request.headers.get("HX-Target") or "").lower()
    if target == "documents-table":
        return await _render_documents_partial(request, db, user, flash, status_code=status_code)
    if target == "youtube-ingest-panel":
        return await _render_youtube_ingest_panel(request, db, user, flash, status_code=status_code)
    flash_kind = "success" if (flash or {}).get("status") == "success" else "error"
    flash_message = (flash or {}).get("message") or "Request completed."
    context = {"request": request, "flash_kind": flash_kind, "flash_message": flash_message}
    return templates.TemplateResponse("documents/partials/flash.html", context, status_code=status_code)


async def _render_youtube_ingest_panel(
    request: Request,
    db,
    user: dict,
    flash: Optional[dict] = None,
    job_id: Optional[str] = None,
    status_code: int = status.HTTP_200_OK,
):
    google_tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    token_map = {str(row.get("integration")).lower(): row for row in (google_tokens or []) if row.get("integration")}
    youtube_connected = "youtube" in token_map
    ingest_job = None
    events: List[Dict[str, Any]] = []
    if job_id:
        ingest_job = await get_job(db, job_id, user["user_id"])
        if ingest_job:
            events = await list_pipeline_events(db, user["user_id"], job_id=job_id, limit=50)
    context = {
        "request": request,
        "csrf_token": _get_csrf_token(request),
        "youtube_connected": youtube_connected,
        "flash": flash,
        "ingest_job": ingest_job,
        "ingest_events": events,
        "content_schema_choices": CONTENT_SCHEMA_CHOICES,
    }
    return templates.TemplateResponse("dashboard/partials/youtube_ingest_panel.html", context, status_code=status_code)


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
    
    # Set flash message and redirect
    await add_flash(request, "Marked seen", category="info")
    return RedirectResponse(url="/dashboard/activity", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/notifications/{notification_id}/dismiss")
async def api_dismiss(notification_id: str, request: Request, user: dict = Depends(get_current_user)):
    # CSRF protection via header token for HTMX
    cookie_token = request.cookies.get("csrf_token")
    header_token = request.headers.get("X-CSRF-Token")
    if cookie_token is None or header_token is None or not hmac.compare_digest(str(cookie_token), str(header_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    db = ensure_db()
    await dismiss_notification(db, user["user_id"], notification_id)
    
    # Set flash message and redirect
    await add_flash(request, "Dismissed", category="info")
    return RedirectResponse(url="/dashboard/activity", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/stream")
async def api_stream(request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    session = getattr(request.state, "session", None)
    return notifications_stream_response(request, db, user, session=session)


@router.get("/api/pipelines/stream")
async def api_pipeline_stream(request: Request, job_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    db = ensure_db()
    return pipeline_stream_response(request, db, user, job_id=job_id)


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
    is_secure = is_secure_request(request, settings)

    session_cookie = request.cookies.get(settings.session_cookie_name)
    if session_cookie:
        try:
            db = ensure_db()
            # Get user_id from session if available for ownership validation
            user_id = None
            session = getattr(request.state, "session", None)
            if session:
                user_id = session.get("user_id")
            await delete_user_session(db, session_cookie, user_id=user_id)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("Failed to delete session on logout: %s", exc)
        # Delete session cookie - use manual header to avoid conflicts
        from datetime import datetime, timezone
        expires = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        session_cookie_value = f'{settings.session_cookie_name}=""; expires={expires.strftime("%a, %d %b %Y %H:%M:%S GMT")}; Max-Age=0; Path=/; SameSite=lax; HttpOnly'
        if is_secure:
            session_cookie_value += "; Secure"
        response.headers.append("Set-Cookie", session_cookie_value)

    # Clear all authentication-related cookies
    # NOTE: Cloudflare Workers only sends ONE Set-Cookie header per response.
    # We prioritize access_token (most important for logout) by deleting it LAST,
    # so it's the Set-Cookie header that gets sent.
    # We use the current request's secure flag, which should match how cookies were set.
    
    # Delete other OAuth cookies first (won't be sent, but keeps code clean)
    response.delete_cookie(COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie(COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_redirect_uri", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_integration", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_redirect_next", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("csrf_token", path="/", samesite="lax", httponly=True, secure=is_secure)
    
    # Delete access_token LAST - this is the Set-Cookie header that will be sent
    # (most important for logout, so it takes priority)
    response.delete_cookie("access_token", path="/", samesite="lax", httponly=True, secure=is_secure)

    return response


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    try:
        if getattr(request.state, "user", None):
            return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        return templates.TemplateResponse("home.html", {"request": request})
    except Exception as exc:
        logger.error(
            "Error rendering home page: %s, error_type=%s",
            str(exc),
            type(exc).__name__,
            exc_info=True,
        )
        # Return a simple error page instead of letting the exception propagate
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error rendering page: {str(exc)}"
        ) from exc


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
async def dashboard(request: Request, user: dict = Depends(get_current_user)):
    """
    Main dashboard page showing job stats, integrations, and ingest forms.
    """
    # Handle DB initialization failures with explicit 503 for protected routes
    try:
        db = ensure_db()
    except HTTPException as exc:
        if exc.status_code == 500:
            logger.error("Dashboard: Database unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service temporarily unavailable"
            ) from exc
        raise
    
    csrf = _get_csrf_token(request)
    
    # Load job stats
    stats: dict = {}
    try:
        stats = await get_job_stats(db, user["user_id"])
    except Exception as exc:
        logger.exception("Failed loading job stats: %s", exc)
        stats = {"queued": 0, "running": 0, "completed": 0}
    
    # Check Google integrations
    google_tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    token_map = {str(row.get("integration")).lower(): row for row in (google_tokens or []) if row.get("integration")}
    drive_connected = "drive" in token_map
    youtube_connected = "youtube" in token_map
    
    context = {
        "request": request,
        "user": user,
        "stats": stats,
        "drive_connected": drive_connected,
        "youtube_connected": youtube_connected,
        "csrf_token": csrf,
        "content_schema_choices": CONTENT_SCHEMA_CHOICES,
        "page_title": "Dashboard",
    }
    
    template_name = "dashboard/index_fragment.html" if _is_htmx(request) else "dashboard/index.html"
    resp = templates.TemplateResponse(template_name, context)
    if not request.cookies.get("csrf_token"):
        is_secure = is_secure_request(request, settings)
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


@router.get("/dashboard/documents", response_class=HTMLResponse)
async def documents_page(request: Request, page: int = 1, user: dict = Depends(get_current_user)):
    # Handle DB initialization failures with explicit 503 for protected routes
    try:
        db = ensure_db()
    except HTTPException as exc:
        if exc.status_code == 500:
            logger.error("Documents page: Database unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service temporarily unavailable"
            ) from exc
        raise
    csrf = _get_csrf_token(request)
    documents, total = await _load_document_views(db, user["user_id"], page=page, page_size=20)
    google_tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    token_map = {str(row.get("integration")).lower(): row for row in (google_tokens or []) if row.get("integration")}
    drive_connected = "drive" in token_map
    youtube_connected = "youtube" in token_map
    drive_workspace = None
    if drive_connected:
        try:
            drive_workspace = await get_drive_workspace(db, user["user_id"])  # type: ignore
            if not drive_workspace:
                logger.info("drive_workspace_missing_sched_provision", extra={"user_id": user["user_id"]})
                task = asyncio.create_task(_provision_workspace_background(db, user["user_id"]))
                _track_task(task)
        except Exception as exc:
            logger.warning(
                "drive_workspace_lookup_failed",
                exc_info=True,
                extra={"user_id": user["user_id"], "error": str(exc)},
            )
            drive_workspace = None
    context = {
        "request": request,
        "user": user,
        "documents": documents,
        "total_documents": total,
        "csrf_token": csrf,
        "drive_connected": drive_connected,
        "youtube_connected": youtube_connected,
        "drive_sync_overview": _drive_sync_overview(documents, drive_connected, drive_workspace),
        "page_title": "Documents",
        "flash": None,
    }
    template_name = "documents/index_fragment.html" if _is_htmx(request) else "documents/index.html"
    resp = templates.TemplateResponse(template_name, context)
    if not request.cookies.get("csrf_token"):
        is_secure = is_secure_request(request, settings)
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
    drive_meta = {}
    metadata = doc_view.get("metadata") or {}
    if isinstance(metadata, dict):
        drive_meta = metadata.get("drive") or {}
    title_hint = (doc_view.get("frontmatter") or {}).get("title") or drive_meta.get("title")

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
        "drive_meta": drive_meta,
        "page_title": title_hint or f"Document {document_id}",
        "content_schema_choices": CONTENT_SCHEMA_CHOICES,
    }
    template_name = "documents/detail_fragment.html" if _is_htmx(request) else "documents/detail.html"
    resp = templates.TemplateResponse(template_name, context)
    if not request.cookies.get("csrf_token"):
        is_secure = is_secure_request(request, settings)
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
    content_type: str = Form("https://schema.org/BlogPosting"),
    instructions: Optional[str] = Form(None),
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
            content_type=content_type.strip() or "https://schema.org/BlogPosting",
            instructions=(instructions or "").strip() or None,
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


@router.post("/dashboard/documents/{document_id}/drive/sync", response_class=HTMLResponse)
async def dashboard_drive_sync(
    document_id: str,
    request: Request,
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    _validate_csrf(request, csrf_token)
    db = ensure_db()
    document = await get_document(db, document_id, user_id=user["user_id"])
    if not document:
        return _render_flash(request, "Document not found", "error", status.HTTP_404_NOT_FOUND)
    metadata = _json_field(document.get("metadata"), {})
    drive_stage = metadata.get("drive_stage")
    try:
        await sync_drive_doc_for_document(
            db,
            user["user_id"],
            document_id,
            {"metadata": {"drive_stage": drive_stage} if drive_stage else {}},
        )
    except Exception as exc:
        logger.exception(
            "drive_manual_sync_failed",
            extra={"document_id": document_id, "user_id": user["user_id"]},
        )
        return _render_flash(request, f"Drive sync failed: {exc}", "error", status.HTTP_502_BAD_GATEWAY)
    return _render_flash(request, "Drive sync started", "success")


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
    if normalized == "google_docs":
        try:
            export_meta = await _export_version_to_drive(db, user["user_id"], doc, version)
            message = f"Google Docs updated • Revision {export_meta.get('revision_id') or 'n/a'}"
            try:
                await notify_activity(
                    db,
                    user["user_id"],
                    "success",
                    message,
                    context={"document_id": document_id, "href": f"/dashboard/documents/{document_id}"},
                )
            except Exception as exc:
                logger.warning(
                    "drive_export_notify_failed",
                    exc_info=True,
                    extra={"document_id": document_id, "user_id": user["user_id"], "error": str(exc)},
                )
            return _render_flash(request, message, "success")
        except HTTPException as exc:
            return _render_flash(request, exc.detail, "error", exc.status_code)
        except Exception:
            logger.error("drive_export_failed", exc_info=True, extra={"document_id": document_id, "doc_hint": "docs/DEPLOYMENT.md#drive-workspace-setup"})
            return _render_flash(request, "Failed to push to Google Docs", "error", status.HTTP_502_BAD_GATEWAY)
    message = f"{label} export queued (version {row.get('version_id')})"
    return _render_flash(request, message, "success")


@router.post("/dashboard/jobs", response_class=HTMLResponse)
async def create_job_html(
    request: Request,
    document_id: str = Form(...),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    _validate_csrf(request, csrf_token)

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
    _validate_csrf(request, csrf_token)
    db = ensure_db()
    source = (drive_source or "").strip()
    if not source:
        flash = {"status": "error", "message": "Drive folder URL or ID is required"}
        return await _render_ingest_response(request, db, user, flash, status_code=status.HTTP_400_BAD_REQUEST)
    try:
        doc = await create_drive_document_for_user(db, user["user_id"], source)
        flash = {"status": "success", "message": f"Registered document {doc.document_id}"}
        return await _render_ingest_response(request, db, user, flash)
    except HTTPException as exc:
        flash = {"status": "error", "message": exc.detail or "Failed to register Drive folder"}
        return await _render_ingest_response(request, db, user, flash, status_code=exc.status_code)
    except Exception:
        logger.exception("Failed to create Drive document via UI")
        flash = {"status": "error", "message": "Unexpected error while registering Drive folder"}
        return await _render_ingest_response(request, db, user, flash, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("/dashboard/documents/youtube", response_class=HTMLResponse)
async def create_youtube_document_form(
    request: Request,
    youtube_url: str = Form(...),
    content_type: str = Form("https://schema.org/BlogPosting"),
    instructions: Optional[str] = Form(None),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    _validate_csrf(request, csrf_token)
    db = ensure_db()
    url = (youtube_url or "").strip()
    if not url:
        flash = {"status": "error", "message": "YouTube URL is required"}
        return await _render_ingest_response(request, db, user, flash, status_code=status.HTTP_400_BAD_REQUEST)
    _, queue = ensure_services()
    schema_value = content_type.strip() or "https://schema.org/BlogPosting"
    autopilot_options = {
        "content_type": schema_value,
        "instructions": (instructions or "").strip() or None,
    }
    try:
        job = await start_ingest_youtube_job(
            db,
            queue,
            user["user_id"],
            url,
            autopilot_options={k: v for k, v in autopilot_options.items() if v},
            autopilot_enabled=True,
        )
        flash = {
            "status": "success",
            "message": f"YouTube ingest job {job.job_id} queued (doc {job.document_id})",
        }
        return await _render_youtube_ingest_panel(request, db, user, flash, job_id=job.job_id)
    except HTTPException as exc:
        flash = {"status": "error", "message": exc.detail or "Failed to ingest YouTube video"}
        return await _render_youtube_ingest_panel(request, db, user, flash, status_code=exc.status_code)
    except Exception:
        logger.exception("Failed to queue YouTube ingest from UI")
        flash = {"status": "error", "message": "Unexpected error while ingesting video"}
        return await _render_youtube_ingest_panel(request, db, user, flash, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("/dashboard/documents/text", response_class=HTMLResponse)
async def create_text_document_form(
    request: Request,
    text_body: str = Form(...),
    title: Optional[str] = Form(None),
    content_type: str = Form("https://schema.org/BlogPosting"),
    instructions: Optional[str] = Form(None),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    _validate_csrf(request, csrf_token)
    db = ensure_db()
    _, queue = ensure_services()
    body = (text_body or "").strip()
    if not body:
        flash = {"status": "error", "message": "Text content is required"}
        return await _render_ingest_response(request, db, user, flash, status_code=status.HTTP_400_BAD_REQUEST)
    schema_value = content_type.strip() or "https://schema.org/BlogPosting"
    autopilot_options = {
        "content_type": schema_value,
        "instructions": (instructions or "").strip() or None,
    }
    try:
        job = await start_ingest_text_job(
            db,
            queue,
            user["user_id"],
            body,
            title,
            autopilot_options={k: v for k, v in autopilot_options.items() if v},
            autopilot_enabled=True,
        )
        flash = {
            "status": "success",
            "message": f"Text ingest job {job.job_id} queued (doc {job.document_id})",
        }
        return await _render_ingest_response(request, db, user, flash)
    except HTTPException as exc:
        flash = {"status": "error", "message": exc.detail or "Failed to ingest text"}
        return await _render_ingest_response(request, db, user, flash, status_code=exc.status_code)
    except Exception:
        logger.exception("Failed to queue text ingest from UI")
        flash = {"status": "error", "message": "Unexpected error while ingesting text"}
        return await _render_ingest_response(request, db, user, flash, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
    template_name = "jobs/detail_fragment.html" if _is_htmx(request) else "jobs/detail.html"
    resp = templates.TemplateResponse(template_name, context)
    if not request.cookies.get("csrf_token"):
        is_secure = is_secure_request(request, settings)
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/dashboard/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, user: dict = Depends(get_current_user), page: int = 1, status: Optional[str] = None):
    # Handle DB initialization failures with explicit 503 for protected routes
    try:
        db = ensure_db()
    except HTTPException as exc:
        if exc.status_code == 500:
            logger.error("Jobs page: Database unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service temporarily unavailable"
            ) from exc
        raise
    csrf = _get_csrf_token(request)
    # Alias status parameter to avoid collision with imported status module
    status_filter_param = status
    status_filter = None
    if status_filter_param:
        try:
            norm = normalize_ui_status(status_filter_param)
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
    template_name = "jobs/index_fragment.html" if _is_htmx(request) else "jobs/index.html"
    resp = templates.TemplateResponse(template_name, context)
    if not request.cookies.get("csrf_token"):
        is_secure = is_secure_request(request, settings)
        resp.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=is_secure)
    return resp


@router.get("/dashboard/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request, user: dict = Depends(get_current_user)):
    db = ensure_db()
    tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    stored_user = await get_user_by_id(db, user["user_id"])  # type: ignore
    github_info = _github_info_for(user, stored_user)
    integrations = _build_integrations_model(tokens, github_info=github_info)
    services_meta = _services_meta_filtered()
    integrations = _filter_integrations_map(integrations)
    csrf = _get_csrf_token(request)
    context = {"request": request, "user": user, "integrations": integrations, "services_meta": services_meta, "page_title": "Integrations", "csrf_token": csrf}
    resp = templates.TemplateResponse("integrations/index.html", context)
    if not request.cookies.get("csrf_token"):
        is_secure = is_secure_request(request, settings)
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
    github_info = _github_info_for(user, None)
    integrations = _build_integrations_model(tokens, github_info=github_info)
    services_meta = _services_meta_filtered()
    integrations = _filter_integrations_map(integrations)
    csrf = _get_csrf_token(request)
    return templates.TemplateResponse("integrations/partials/grid.html", {"request": request, "integrations": integrations, "services_meta": services_meta, "csrf_token": csrf})


@router.get("/dashboard/integrations/{service}", response_class=HTMLResponse)
async def integration_detail(service: str, request: Request, user: dict = Depends(get_current_user)):
    # Validate service early to avoid unnecessary DB calls and object construction
    if service not in ALLOWED_INTEGRATION_KEYS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    db = ensure_db()
    tokens = await list_google_tokens(db, user["user_id"])  # type: ignore
    stored_user = await get_user_by_id(db, user["user_id"])  # type: ignore
    github_info = _github_info_for(user, stored_user)
    integrations = _build_integrations_model(tokens, github_info=github_info)
    services_meta = SERVICES_META
    csrf = _get_csrf_token(request)
    drive_overview = None
    drive_workspace = None
    if service == "drive":
        is_connected = bool(integrations.get("drive", {}).get("connected"))
        if is_connected:
            try:
                drive_workspace = await get_drive_workspace(db, user["user_id"])  # type: ignore
                if not drive_workspace:
                    logger.info("drive_workspace_missing_sched_provision", extra={"user_id": user["user_id"]})
                    task = asyncio.create_task(_provision_workspace_background(db, user["user_id"]))
                    _track_task(task)
            except Exception as exc:
                logger.warning("drive_workspace_provision_failed", exc_info=True, extra={"user_id": user["user_id"], "error": str(exc)})
        docs, _ = await _load_document_views(db, user["user_id"], page=1, page_size=50)
        drive_overview = _drive_sync_overview(docs, is_connected, drive_workspace)
    return templates.TemplateResponse(
        "integrations/detail.html",
        {
            "request": request,
            "user": user,
            "service": services_meta[service],
            "integration": integrations.get(service),
            "csrf_token": csrf,
            "drive_overview": drive_overview,
        }
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
    
    # Set flash message and redirect
    await add_flash(request, "Job retried", category="success")
    if _is_htmx(request):
        # For HTMX, redirect to jobs page
        return RedirectResponse(url="/dashboard/jobs", status_code=status.HTTP_303_SEE_OTHER)
    # For regular requests, redirect to job detail
    return RedirectResponse(url=f"/dashboard/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


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
    
    # Set flash message and redirect
    await add_flash(request, "Job cancelled", category="success")
    if _is_htmx(request):
        # For HTMX, redirect to jobs page
        return RedirectResponse(url="/dashboard/jobs", status_code=status.HTTP_303_SEE_OTHER)
    # For regular requests, redirect to job detail
    return RedirectResponse(url=f"/dashboard/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


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
        is_secure = is_secure_request(request, settings)
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
    return await _render_account_page(request, user)


@router.post("/dashboard/account/ai", response_class=HTMLResponse)
async def update_account_ai_preferences(
    request: Request,
    tone: str = Form("informative"),
    model: str = Form("gpt-5.1"),
    max_sections: int = Form(5),
    target_chapters: int = Form(4),
    temperature: float = Form(0.6),
    include_images: Optional[str] = Form("on"),
    csrf_token: str = Form(...),
    user: dict = Depends(get_current_user),
):
    _validate_csrf(request, csrf_token)
    tone = (tone or "").strip()
    if tone and len(tone) < 3:
        return await _render_account_page(
            request,
            user,
            ai_error="Tone must be at least 3 characters.",
        )
    if tone and len(tone) > 60:
        tone = tone[:60]
    # Resolve the configured OpenAI blog model; GPT-5.1 is the current target default.
    # See https://platform.openai.com/docs/models/gpt-5.1
    configured_model = settings.openai_blog_model or "gpt-5.1"
    selected_model = (model or "").strip() or configured_model
    available_models = {choice["value"] for choice in get_ai_model_choices()}
    if selected_model not in available_models:
        selected_model = configured_model
    include_images_flag = _form_bool(include_images)
    db = ensure_db()
    current = await get_user_preferences(db, user["user_id"])
    merged = set_ai_preferences(
        current,
        {
            "tone": tone or None,
            "model": selected_model,
            "max_sections": max(1, min(int(max_sections), 12)),
            "target_chapters": max(1, min(int(target_chapters), 12)),
            "temperature": float(temperature),
            "include_images": include_images_flag,
        },
    )
    await update_user_preferences(db, user["user_id"], merged)
    # Update request.state.user preferences best-effort so subsequent renders include latest data
    stored_user = dict(user)
    stored_user["preferences"] = merged
    request.state.user = stored_user
    return await _render_account_page(
        request,
        stored_user,
        ai_message="AI defaults updated.",
    )

@router.post("/dashboard/account/delete")
async def delete_account(
    request: Request,
    csrf_token: str = Form(...),
    confirmation: str = Form(...),
    user: dict = Depends(get_current_user),
):
    cookie_token = request.cookies.get("csrf_token")
    if cookie_token is None or not secrets.compare_digest(str(cookie_token), str(csrf_token)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    normalized = confirmation.strip()
    if normalized.upper() != "DELETE":
        return await _render_account_page(
            request,
            user,
            status_code=status.HTTP_400_BAD_REQUEST,
            delete_error="Type DELETE to confirm account deletion.",
            delete_value=confirmation,
        )

    # DB-safe: catch DB initialization failures and return graceful error
    try:
        db = ensure_db()
        deleted = await delete_user_account(db, user["user_id"])  # type: ignore[index]
    except HTTPException as exc:
        if exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR:
            # Database temporarily unavailable - return graceful error
            return await _render_account_page(
                request,
                user,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                delete_error="The database is temporarily unavailable. Please try again in a few minutes.",
            )
        raise
    except Exception as exc:
        # Other unexpected errors - log and return generic error
        logger.error("Unexpected error during account deletion: %s", exc, exc_info=True)
        return await _render_account_page(
            request,
            user,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            delete_error="An unexpected error occurred. Please try again or contact support.",
        )

    if not deleted:
        return await _render_account_page(
            request,
            user,
            status_code=status.HTTP_400_BAD_REQUEST,
            delete_error="We couldn't delete your account. Please try again or contact support.",
        )

    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    is_secure = is_secure_request(request, settings)
    # Session deletion is handled by FK cascade when user is deleted
    # We only need to clear the cookie from the browser
    # NOTE: Cloudflare Workers only sends ONE Set-Cookie header per response.
    # We prioritize access_token (most important) by deleting it LAST.
    session_cookie = request.cookies.get(settings.session_cookie_name)
    if session_cookie:
        response.delete_cookie(
            settings.session_cookie_name,
            path="/",
            samesite="lax",
            httponly=True,
            secure=is_secure,
        )
    # Delete other cookies first (won't be sent, but keeps code clean)
    response.delete_cookie(COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie(COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_redirect_uri", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_integration", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("google_redirect_next", path="/", samesite="lax", httponly=True, secure=is_secure)
    response.delete_cookie("csrf_token", path="/", samesite="lax", httponly=True, secure=is_secure)
    # Delete access_token LAST - this is the Set-Cookie header that will be sent
    response.delete_cookie("access_token", path="/", samesite="lax", httponly=True, secure=is_secure)
    return response
