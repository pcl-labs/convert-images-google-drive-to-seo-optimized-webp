MAX_TEXT_LENGTH = 20000  # configurable upper bound for text ingestion
from fastapi import APIRouter, Request, Depends, HTTPException, status
from fastapi.responses import RedirectResponse, PlainTextResponse
from typing import Optional
import uuid
import secrets
import json
from datetime import datetime, timezone

from .config import settings
from .models import (
    JobStatus,
    JobProgress,
    JobListResponse,
    UserResponse,
    APIKeyResponse,
    StatsResponse,
    JobStatusEnum,
    JobType,
    IngestYouTubeRequest,
    IngestTextRequest,
    DriveDocumentRequest,
    OptimizeDocumentRequest,
    GenerateBlogRequest,
    Document,
    DocumentVersionSummary,
    DocumentVersionList,
    DocumentVersionDetail,
    DocumentExportRequest,
    DocumentExportResponse,
)
from .database import (
    create_job_extended,
    get_job,
    list_jobs,
    get_job_stats,
    update_job_status,
    get_google_tokens,
    get_user_by_id,
    create_document,
    get_document,
    list_document_versions,
    get_document_version,
    create_document_export,
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
from core.url_utils import parse_youtube_video_id
from .database import get_usage_summary, list_usage_events, count_usage_events
from fastapi import Query

logger = get_logger(__name__)

router = APIRouter()


def _parse_job_progress_model(progress_str: str) -> JobProgress:
    data = parse_job_progress(progress_str) or {}
    return JobProgress(**data)


def _parse_db_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _coerce_document_metadata(doc: dict) -> dict:
    metadata = doc.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if metadata is None:
        metadata = {}
    doc["metadata"] = metadata
    return doc


def _json_field(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    if value is None:
        return default
    return value


def _serialize_document(doc: dict) -> Document:
    parsed = _coerce_document_metadata(dict(doc))
    return Document(
        document_id=parsed.get("document_id"),
        user_id=parsed.get("user_id"),
        source_type=parsed.get("source_type"),
        source_ref=parsed.get("source_ref"),
        raw_text=parsed.get("raw_text"),
        metadata=parsed.get("metadata"),
        content_format=parsed.get("content_format"),
        frontmatter=_json_field(parsed.get("frontmatter"), {}),
        latest_version_id=parsed.get("latest_version_id"),
        created_at=_parse_db_datetime(parsed.get("created_at")),
        updated_at=_parse_db_datetime(parsed.get("updated_at")),
    )


def _version_payload(row: dict) -> dict:
    return {
        "version_id": row.get("version_id"),
        "document_id": row.get("document_id"),
        "version": row.get("version"),
        "content_format": row.get("content_format"),
        "frontmatter": _json_field(row.get("frontmatter"), {}),
        "body_mdx": row.get("body_mdx"),
        "body_html": row.get("body_html"),
        "outline": _json_field(row.get("outline"), []),
        "chapters": _json_field(row.get("chapters"), []),
        "sections": _json_field(row.get("sections"), []),
        "assets": _json_field(row.get("assets"), {}),
        "created_at": _parse_db_datetime(row.get("created_at")),
    }


def _version_summary_model(row: dict) -> DocumentVersionSummary:
    data = _version_payload(row)
    return DocumentVersionSummary(
        version_id=data["version_id"],
        document_id=data["document_id"],
        version=data["version"],
        content_format=data["content_format"],
        frontmatter=data["frontmatter"],
        created_at=data["created_at"],
    )


def _version_detail_model(row: dict) -> DocumentVersionDetail:
    data = _version_payload(row)
    return DocumentVersionDetail(**data)


async def _load_document_for_user(db, document_id: str, user_id: str) -> dict:
    doc = await get_document(db, document_id, user_id=user_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return _coerce_document_metadata(doc)


def _drive_folder_from_document(doc: dict) -> str:
    if doc.get("source_type") not in {"drive", "drive_folder"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Document is not backed by a Drive folder")
    folder_id = doc.get("source_ref") or doc["metadata"].get("drive_folder_id")
    if not folder_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Document is missing Drive folder metadata")
    return folder_id

async def create_drive_document_for_user(db, user_id: str, drive_source: str) -> Document:
    # Import locally to avoid hard dependency issues if google client is absent in some envs
    try:
        from googleapiclient.errors import HttpError  # type: ignore
    except Exception:
        class _GoogleHttpError(Exception):
            pass
        HttpError = _GoogleHttpError  # type: ignore

    try:
        service = await build_drive_service_for_user(db, user_id)  # type: ignore
        folder_id = extract_folder_id_from_input(drive_source, service=service)
    except (ValueError, HttpError) as e:
        logger.error("drive_folder_prepare_error", exc_info=True, extra={"user_id": user_id, "drive_source": drive_source})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google not linked or folder not accessible") from None
    except HTTPException:
        raise
    except Exception as e:
        logger.error("drive_folder_unexpected_error", exc_info=True, extra={"user_id": user_id, "drive_source": drive_source})
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create Drive document") from None
    document_id = str(uuid.uuid4())
    doc = await create_document(
        db,
        document_id=document_id,
        user_id=user_id,
        source_type="drive",
        source_ref=folder_id,
        raw_text=None,
        metadata={"input": drive_source},
    )
    return _serialize_document(doc)


async def start_ingest_youtube_job(db, queue, user_id: str, url: str) -> JobStatus:
    clean_url = (url or "").strip()
    video_id = parse_youtube_video_id(clean_url)
    if not video_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid YouTube URL")
    job_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())
    await create_document(
        db,
        document_id,
        user_id,
        source_type="youtube",
        source_ref=video_id,
        raw_text=None,
        metadata={"url": clean_url},
    )
    job_row = await create_job_extended(db, job_id, user_id, job_type=JobType.INGEST_YOUTUBE.value, document_id=document_id)
    payload = {
        "job_id": job_id,
        "user_id": user_id,
        "job_type": JobType.INGEST_YOUTUBE.value,
        "document_id": document_id,
        "youtube_video_id": video_id,
    }
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(queue, job_id, user_id, payload, allow_inline_fallback=False)
    if should_fail:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Queue unavailable or enqueue failed; background processing is required in production.")
    if not enqueued:
        logger.warning(
            "YouTube ingestion job created but not enqueued",
            extra={"job_id": job_id, "document_id": document_id, "reason": "queue unavailable", "exception": str(enqueue_exception) if enqueue_exception else None},
        )
    progress = _parse_job_progress_model(progress_str=job_row.get("progress", "{}"))
    return JobStatus(
        job_id=job_id,
        user_id=user_id,
        status=JobStatusEnum.PENDING,
        progress=progress,
        created_at=_parse_db_datetime(job_row.get("created_at")),
        job_type=JobType.INGEST_YOUTUBE.value,
        document_id=document_id,
    )


async def start_ingest_text_job(db, queue, user_id: str, text: str, title: Optional[str] = None) -> JobStatus:
    clean_text = (text or "").strip()
    if not clean_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Text is required")
    if len(clean_text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Text must be at most {MAX_TEXT_LENGTH} characters")
    job_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())
    metadata = {"title": title} if title else {}
    await create_document(
        db,
        document_id,
        user_id,
        source_type="text",
        source_ref=None,
        raw_text=clean_text,
        metadata=metadata,
    )
    job_row = await create_job_extended(db, job_id, user_id, job_type=JobType.INGEST_TEXT.value, document_id=document_id)
    payload = {"job_id": job_id, "user_id": user_id, "job_type": JobType.INGEST_TEXT.value, "document_id": document_id}
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(queue, job_id, user_id, payload, allow_inline_fallback=False)
    if should_fail:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Queue unavailable or enqueue failed; background processing is required in production.")
    if not enqueued:
        logger.warning(
            "Text ingestion job created but not enqueued",
            extra={"job_id": job_id, "document_id": document_id, "reason": "queue unavailable", "exception": str(enqueue_exception) if enqueue_exception else None},
        )
    progress = _parse_job_progress_model(progress_str=job_row.get("progress", "{}"))
    return JobStatus(
        job_id=job_id,
        user_id=user_id,
        status=JobStatusEnum.PENDING,
        progress=progress,
        created_at=_parse_db_datetime(job_row.get("created_at")),
        job_type=JobType.INGEST_TEXT.value,
        document_id=document_id,
    )


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


@router.post("/api/v1/documents/drive", response_model=Document, tags=["Documents"])
async def create_drive_document_endpoint(req: DriveDocumentRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    return await create_drive_document_for_user(db, user["user_id"], req.drive_source)


@router.get("/api/v1/documents/{document_id}/versions", response_model=DocumentVersionList, tags=["Documents"])
async def list_document_versions_endpoint(document_id: str, limit: int = 25, user: dict = Depends(get_current_user)):
    db = ensure_db()
    await _load_document_for_user(db, document_id, user["user_id"])
    rows = await list_document_versions(db, document_id, user["user_id"], limit=max(1, min(limit, 50)))
    return DocumentVersionList(versions=[_version_summary_model(row) for row in rows])


@router.get("/api/v1/documents/{document_id}/versions/{version_id}", response_model=DocumentVersionDetail, tags=["Documents"])
async def get_document_version_endpoint(document_id: str, version_id: str, user: dict = Depends(get_current_user)):
    db = ensure_db()
    await _load_document_for_user(db, document_id, user["user_id"])
    row = await get_document_version(db, document_id, version_id, user["user_id"])
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return _version_detail_model(row)


@router.get("/api/v1/documents/{document_id}/versions/{version_id}/body", tags=["Documents"])
async def get_document_version_body(document_id: str, version_id: str, format: str = "mdx", user: dict = Depends(get_current_user)):
    db = ensure_db()
    await _load_document_for_user(db, document_id, user["user_id"])
    row = await get_document_version(db, document_id, version_id, user["user_id"])
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    version = _version_detail_model(row)
    fmt = format.lower()
    if fmt == "html":
        body = version.body_html or ""
        media_type = "text/html"
        ext = "html"
    else:
        body = version.body_mdx or ""
        media_type = "text/plain"
        ext = "mdx"
    if not body:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{fmt.upper()} body unavailable")
    headers = {"Content-Disposition": f'attachment; filename=\"{document_id}-{version_id}.{ext}\"'}
    return PlainTextResponse(body, media_type=media_type, headers=headers)


@router.post("/api/v1/documents/{document_id}/exports", response_model=DocumentExportResponse, tags=["Documents"])
async def create_document_export_endpoint(document_id: str, request: DocumentExportRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    doc = await _load_document_for_user(db, document_id, user["user_id"])
    version_id = request.version_id or doc.get("latest_version_id")
    if not version_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No version available for export")
    version = await get_document_version(db, document_id, version_id, user["user_id"])
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    row = await create_document_export(
        db,
        document_id=document_id,
        version_id=version_id,
        user_id=user["user_id"],
        target=request.target.value,
        payload=request.metadata,
    )
    return DocumentExportResponse(
        export_id=row.get("export_id"),
        status=row.get("status"),
        target=request.target,
        version_id=row.get("version_id"),
        document_id=row.get("document_id"),
        created_at=_parse_db_datetime(row.get("created_at")),
    )


async def start_optimize_job(
    db,
    queue,
    user_id: str,
    document_id: str,
    request: OptimizeDocumentRequest,
) -> JobStatus:
    doc = await _load_document_for_user(db, document_id, user_id)
    folder_id = _drive_folder_from_document(doc)
    job_id = str(uuid.uuid4())
    job_row = await create_job_extended(
        db,
        job_id,
        user_id,
        job_type=JobType.OPTIMIZE_DRIVE.value,
        document_id=document_id,
        extensions=request.extensions or [],
    )
    payload = {
        "job_id": job_id,
        "user_id": user_id,
        "job_type": JobType.OPTIMIZE_DRIVE.value,
        "document_id": document_id,
        "extensions": request.extensions or [],
        "overwrite": request.overwrite,
        "skip_existing": request.skip_existing,
        "cleanup_originals": request.cleanup_originals,
        "max_retries": request.max_retries,
    }
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(queue, job_id, user_id, payload, allow_inline_fallback=False)
    if should_fail:
        detail = "Queue unavailable or enqueue failed; background processing is required in production."
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    if not enqueued:
        logger.warning(
            f"Job {job_id} created but not enqueued. Job will remain in pending state.",
            extra={
                "job_id": job_id,
                "event": "job.enqueue_failed",
                "reason": "queue unavailable or enqueue failed",
                "enqueue_exception": (str(enqueue_exception) if enqueue_exception else None),
            },
        )
    progress = _parse_job_progress_model(progress_str=job_row.get("progress", "{}"))
    return JobStatus(
        job_id=job_id,
        user_id=user_id,
        status=JobStatusEnum.PENDING,
        progress=progress,
        created_at=_parse_db_datetime(job_row.get("created_at")),
        job_type=JobType.OPTIMIZE_DRIVE.value,
        document_id=document_id,
    )


async def start_generate_blog_job(
    db,
    queue,
    user_id: str,
    req: GenerateBlogRequest,
) -> JobStatus:
    document_id = req.document_id
    doc = await _load_document_for_user(db, document_id, user_id)
    text = (doc.get("raw_text") or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Document is missing raw text to generate from")
    job_id = str(uuid.uuid4())
    job_row = await create_job_extended(
        db,
        job_id,
        user_id,
        job_type=JobType.GENERATE_BLOG.value,
        document_id=document_id,
    )
    payload = {
        "job_id": job_id,
        "user_id": user_id,
        "job_type": JobType.GENERATE_BLOG.value,
        "document_id": document_id,
        "options": req.options.model_dump(),
    }
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(queue, job_id, user_id, payload, allow_inline_fallback=False)
    if should_fail:
        detail = "Queue unavailable or enqueue failed; background processing is required in production."
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    if not enqueued:
        logger.warning(
            "generate_blog_job_enqueued_false",
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "event": "job.enqueue_failed",
                "reason": "queue unavailable or enqueue failed",
                "enqueue_exception": (str(enqueue_exception) if enqueue_exception else None),
            },
        )
    progress = _parse_job_progress_model(progress_str=job_row.get("progress", "{}"))
    return JobStatus(
        job_id=job_id,
        user_id=user_id,
        status=JobStatusEnum.PENDING,
        progress=progress,
        created_at=_parse_db_datetime(job_row.get("created_at")),
        job_type=JobType.GENERATE_BLOG.value,
        document_id=document_id,
    )


@router.post("/api/v1/optimize", response_model=JobStatus, tags=["Jobs"])
async def optimize_images(request: OptimizeDocumentRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    queue = ensure_services()[1]
    try:
        return await start_optimize_job(db, queue, user["user_id"], request.document_id, request)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create job: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create optimization job")


@router.post("/api/v1/pipelines/generate_blog", response_model=JobStatus, tags=["Pipelines"])
async def generate_blog_pipeline(request: GenerateBlogRequest, user: dict = Depends(get_current_user)):
    db, queue = ensure_services()
    try:
        return await start_generate_blog_job(db, queue, user["user_id"], request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to start generate_blog pipeline", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create generate_blog job") from None


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
        created_at=_parse_db_datetime(job.get("created_at")),
        completed_at=_parse_db_datetime(job.get("completed_at")) if job.get("completed_at") else None,
        error=job.get("error"),
        job_type=job.get("job_type"),
        document_id=job.get("document_id"),
        output=(job.get("output") if isinstance(job.get("output"), dict) else None),
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
                created_at=_parse_db_datetime(job.get("created_at")),
                completed_at=_parse_db_datetime(job.get("completed_at")) if job.get("completed_at") else None,
                error=job.get("error"),
                job_type=job.get("job_type"),
                document_id=job.get("document_id"),
                output=(job.get("output") if isinstance(job.get("output"), dict) else None),
            )
        )
    return JobListResponse(jobs=job_statuses, total=total, page=page, page_size=page_size, has_more=(page * page_size) < total)


@router.post("/ingest/youtube", response_model=JobStatus, tags=["Ingestion"])
async def ingest_youtube(req: IngestYouTubeRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    queue = ensure_services()[1]
    return await start_ingest_youtube_job(db, queue, user["user_id"], str(req.url))


@router.post("/ingest/text", response_model=JobStatus, tags=["Ingestion"])
async def ingest_text(req: IngestTextRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    queue = ensure_services()[1]
    return await start_ingest_text_job(db, queue, user["user_id"], req.text, req.title)


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


@router.get("/api/v1/usage/summary", tags=["Usage"]) 
async def get_usage_summary_endpoint( 
    window: int = Query(7, description="Aggregation window in days (1-365)"), 
    user: dict = Depends(get_current_user), 
): 
    """Get usage summary for the current user.""" 
    # Validate input bounds before doing any DB work
    if not isinstance(window, int):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="window must be an integer")
    if window < 1 or window > 365:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="window must be between 1 and 365 days")

    try:
        db = ensure_db() 
        summary = await get_usage_summary(db, user["user_id"], window_days=int(window)) 
        return summary 
    except HTTPException:
        raise
    except Exception:
        # Log internal error, do not leak details
        logger.exception("usage_summary_error")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load usage summary") from None 


@router.get("/api/v1/usage/events", tags=["Usage"]) 
async def get_usage_events_endpoint( 
    limit: int = Query(50, description="Max events to return (1-100)"), 
    offset: int = Query(0, description="Offset for pagination (>=0)"), 
    user: dict = Depends(get_current_user), 
): 
    """List usage events for the current user.""" 
    # Validate inputs
    if not isinstance(limit, int) or not isinstance(offset, int):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="limit and offset must be integers")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="limit must be between 1 and 100")
    if offset < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="offset must be >= 0")

    try:
        db = ensure_db() 
        total = await count_usage_events(db, user["user_id"]) 
        events = await list_usage_events(db, user["user_id"], limit=limit, offset=offset) 
        has_more = (offset + len(events)) < (total or 0)
        return {"events": events, "limit": limit, "offset": offset, "total": total, "has_more": has_more} 
    except HTTPException:
        raise
    except Exception:
        logger.exception("usage_events_error")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load usage events") from None
