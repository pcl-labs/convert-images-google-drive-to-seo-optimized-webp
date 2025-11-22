MAX_TEXT_LENGTH = 20000  # configurable upper bound for text ingestion
from fastapi import APIRouter, Request, Depends, HTTPException, status
from fastapi.responses import RedirectResponse, PlainTextResponse
from typing import Optional, Dict, Any, Tuple
import uuid
import secrets
import json
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse
import re

from .config import settings
from .exceptions import DatabaseError
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
    GenerateBlogOptions,
    Document,
    DocumentVersionSummary,
    DocumentVersionList,
    DocumentVersionDetail,
    DocumentExportRequest,
    DocumentExportResponse,
    CreateProjectRequest,
    ProjectResponse,
    TranscriptResponse,
    ChunkAndEmbedResponse,
    TranscriptSearchRequest,
    TranscriptSearchResponse,
    ProjectGenerateBlogRequest,
    ProjectBlog,
    GenerateProjectBlogResponse,
    ProjectSectionSummary,
    ProjectSectionListResponse,
    ProjectSectionDetail,
    PatchSectionRequest,
    PatchSectionResponse,
    ProjectVersionSummary,
    ProjectVersionsResponse,
    ProjectVersionDetail,
    ProjectBlogDiff,
)
from .database import (
    create_job_extended,
    get_job,
    list_jobs,
    get_job_stats,
    update_job_status,
    list_google_tokens,
    get_user_by_id,
    create_document,
    get_document,
    list_document_versions,
    get_document_version,
    create_document_export,
    set_job_output,
    record_pipeline_event,
    get_user_preferences,
    create_project,
    get_project,
    update_project_status,
    create_transcript_chunk,
    list_transcript_chunks,
)
from .notifications import notify_job
from .auth import create_user_api_key
from .google_oauth import (
    get_google_oauth_url,
    exchange_google_code,
    build_drive_service_for_user,
    build_youtube_service_for_user,
    normalize_google_integration,
    parse_google_scope_list,
)
from .constants import COOKIE_GOOGLE_OAUTH_STATE
from .app_logging import get_logger
from .exceptions import JobNotFoundError
from core.drive_utils import extract_folder_id_from_input
from core.google_clients import GoogleAPIError
from core.youtube_api import fetch_video_metadata, fetch_video_metadata_async, YouTubeAPIError
from core.youtube_captions import YouTubeCaptionsError
from .simple_http import AsyncSimpleClient, HTTPStatusError, RequestError
from .deps import (
    ensure_db,
    ensure_services,
    get_current_user,
    parse_job_progress,
)
from core.url_utils import parse_youtube_video_id
from .drive_workspace import ensure_drive_workspace, ensure_document_drive_structure, link_document_drive_workspace
from .drive_docs import sync_drive_doc_for_document
from .youtube_ingest import ingest_youtube_document, build_outline_from_chapters
from core.transcript_chunking import chunk_transcript
from core.embeddings import embed_texts
from core.vectorize_client import store_embeddings, query_project_chunks
from consumer import process_generate_blog_job
from .database import get_usage_summary, list_usage_events, count_usage_events
from fastapi import Query
from .ai_preferences import resolve_generate_blog_options
from core.sections import (
    extract_sections_from_version,
    find_section_by_id,
    get_latest_version_for_project,
    _word_count,
)
import difflib

logger = get_logger(__name__)

router = APIRouter(
    dependencies=[Depends(get_current_user)],
)


def _validate_redirect_path(path: str, fallback: str) -> str:
    """
    Validate a redirect path to ensure it's a safe relative path.
    Rejects protocol-relative URLs (//evil.com) and absolute URLs (https://evil.com).
    Returns the validated path or the fallback if validation fails.
    """
    if not path:
        return fallback
    
    # Parse the URL to check for netloc (domain/host)
    parsed = urlparse(path)
    
    # Reject if netloc is present (absolute URL or protocol-relative URL)
    if parsed.netloc:
        return fallback
    
    # Reject if path doesn't start with a single "/" (e.g., "//evil.com")
    if not path.startswith("/") or path.startswith("//"):
        return fallback
    
    return path


def _redact_http_body_for_logging(body: Optional[str]) -> str:
    text = (body or "")
    if not text:
        return ""

    # Redact common secret-like patterns (API keys, tokens, emails, long hex/base64, auth headers, file paths)
    patterns = [
        r"sk-[A-Za-z0-9]{20,}",  # API keys
        r"(?:api|auth|session|access|refresh)_?token[=:\s]+[A-Za-z0-9._-]{10,}",
        r"Bearer\s+[A-Za-z0-9._-]{10,}",
        r"[A-Fa-f0-9]{32,}",  # long hex strings
        r"[A-Za-z0-9+/]{32,}={0,2}",  # base64-like
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",  # emails
        r"Authorization:[^\n]+",
        r"(?:/|[A-Za-z]:\\)[^\s]{10,}",  # file paths
    ]

    redacted = text
    for pattern in patterns:
        redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)

    if len(redacted) > 200:
        redacted = redacted[:200]

    return redacted


def _parse_job_progress_model(progress_str: str) -> JobProgress:
    data = parse_job_progress(progress_str) or {}
    return JobProgress(**data)


async def enqueue_job_with_guard(
    queue: Any,
    job_id: str,
    user_id: str,
    payload: Dict[str, Any],
    allow_inline_fallback: bool = False,
) -> Tuple[bool, Optional[Exception], bool]:
    """
    Enqueue a job with environment-aware error handling.
    
    Returns:
        (enqueued: bool, exception: Optional[Exception], should_fail: bool)
        - enqueued: True if successfully enqueued
        - exception: Exception if enqueue failed, None otherwise
        - should_fail: True if the caller should raise an HTTPException (production mode)
    """
    from .cloudflare_queue import QueueProducer
    
    if not isinstance(queue, QueueProducer):
        # Fallback: try to use queue directly if it's a QueueLike
        try:
            await queue.send(payload)
            return True, None, False
        except Exception as e:
            logger.error("Queue send failed", exc_info=True, extra={"job_id": job_id})
            should_fail = settings.environment == "production" and not allow_inline_fallback
            return False, e, should_fail
    
    try:
        enqueued = await queue.send_generic(payload)
        if enqueued:
            return True, None, False
        else:
            should_fail = settings.environment == "production" and not allow_inline_fallback
            return False, None, should_fail
    except Exception as e:
        logger.error("Queue send failed", exc_info=True, extra={"job_id": job_id})
        should_fail = settings.environment == "production" and not allow_inline_fallback
        return False, e, should_fail


def _summarize_google_tokens(rows: list[dict]) -> dict:
    summary: dict[str, dict] = {}
    for row in rows:
        integration = row.get("integration")
        if not integration:
            continue
        summary[integration] = {
            "expiry": row.get("expiry"),
            "scopes": parse_google_scope_list(row.get("scopes")),
            "updated_at": row.get("updated_at"),
        }
    return summary


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
    try:
        service = await build_drive_service_for_user(db, user_id)  # type: ignore
        folder_id = extract_folder_id_from_input(drive_source, service=service)
    except (ValueError, GoogleAPIError):
        logger.error("drive_folder_prepare_error", exc_info=True, extra={"user_id": user_id, "drive_source": drive_source})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google not linked or folder not accessible") from None
    except HTTPException:
        raise
    except Exception:
        logger.error("drive_folder_unexpected_error", exc_info=True, extra={"user_id": user_id, "drive_source": drive_source})
        raise
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


async def start_ingest_drive_job(db, queue, user_id: str, document_id: str) -> JobStatus:
    """Start a Drive ingest job for an existing document."""
    document = await get_document(db, document_id, user_id=user_id)
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    
    # Parse metadata from document
    raw_metadata = document.get("metadata")
    if isinstance(raw_metadata, str):
        try:
            metadata = json.loads(raw_metadata)
        except (json.JSONDecodeError, ValueError):
            metadata = {}
    elif isinstance(raw_metadata, dict):
        metadata = raw_metadata
    else:
        metadata = {}
    
    drive_block = metadata.get("drive") if isinstance(metadata, dict) else {}
    drive_file_id = document.get("drive_file_id") or (drive_block.get("file_id") if isinstance(drive_block, dict) else None)
    
    if not drive_file_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Document does not have an associated Drive file"
        )
    
    job_id = str(uuid.uuid4())
    job_row = await create_job_extended(
        db,
        job_id,
        user_id,
        job_type=JobType.INGEST_DRIVE.value,
        document_id=document_id,
        payload={"drive_file_id": drive_file_id},
    )
    
    payload = {
        "job_id": job_id,
        "user_id": user_id,
        "job_type": JobType.INGEST_DRIVE.value,
        "document_id": document_id,
        "drive_file_id": drive_file_id,
    }
    
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(queue, job_id, user_id, payload, allow_inline_fallback=False)
    if should_fail:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Queue unavailable or enqueue failed; background processing is required in production.")
    if not enqueued:
        logger.warning(
            "Drive ingestion job created but not enqueued",
            extra={"job_id": job_id, "document_id": document_id, "reason": "queue unavailable", "exception": str(enqueue_exception) if enqueue_exception else None},
        )
    
    progress = _parse_job_progress_model(progress_str=job_row.get("progress", "{}"))
    return JobStatus(
        job_id=job_id,
        user_id=user_id,
        status=JobStatusEnum.PENDING,
        progress=progress,
        created_at=_parse_db_datetime(job_row.get("created_at")),
        job_type=JobType.INGEST_DRIVE.value,
        document_id=document_id,
    )


@router.post("/api/v1/drive/watch/renew", response_model=JobStatus)
async def start_drive_watch_renewal_job(user: dict = Depends(get_current_user)):
    db, queue = ensure_services()
    job_id = str(uuid.uuid4())
    job_row = await create_job_extended(
        db,
        job_id,
        user["user_id"],
        job_type=JobType.DRIVE_WATCH_RENEWAL.value,
        payload={},
    )
    payload = {
        "job_id": job_id,
        "user_id": user["user_id"],
        "job_type": JobType.DRIVE_WATCH_RENEWAL.value,
    }
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(
        queue,
        job_id,
        user["user_id"],
        payload,
        allow_inline_fallback=False,
    )
    if should_fail:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Queue unavailable or enqueue failed")
    if not enqueued:
        logger.warning(
            "drive_watch_renewal_not_enqueued",
            extra={"job_id": job_id, "user_id": user["user_id"], "error": str(enqueue_exception) if enqueue_exception else None},
        )
    progress = _parse_job_progress_model(progress_str=job_row.get("progress", "{}"))
    return JobStatus(
        job_id=job_id,
        user_id=user["user_id"],
        status=JobStatusEnum.PENDING,
        progress=progress,
        created_at=_parse_db_datetime(job_row.get("created_at")),
        job_type=JobType.DRIVE_WATCH_RENEWAL.value,
    )


async def start_ingest_youtube_job(
    db,
    queue,
    user_id: str,
    url: str,
    *,
    autopilot_options: Optional[Dict[str, Any]] = None,
    autopilot_enabled: bool = True,
) -> JobStatus:
    clean_url = (url or "").strip()
    video_id = parse_youtube_video_id(clean_url)
    if not video_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid YouTube URL")
    try:
        youtube_service = await build_youtube_service_for_user(db, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        # Use async fetch path in Workers runtime to avoid urllib timeouts
        metadata_bundle = await fetch_video_metadata_async(youtube_service, video_id)
    except YouTubeAPIError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    youtube_meta = metadata_bundle.get("metadata") or {}
    frontmatter = metadata_bundle.get("frontmatter") or {}
    frontmatter.setdefault("source", "youtube")
    if "slug" not in frontmatter:
        frontmatter["slug"] = f"yt-{video_id}"
    youtube_meta["url"] = clean_url
    doc_meta = {
        "url": clean_url,
        "source": "youtube",
        "youtube": youtube_meta,
        "title": frontmatter.get("title"),
        "duration_seconds": youtube_meta.get("duration_seconds"),
    }
    raw_chapters = youtube_meta.get("chapters")
    if isinstance(raw_chapters, list):
        seeded_outline = build_outline_from_chapters(raw_chapters)
        if seeded_outline:
            doc_meta["latest_outline"] = seeded_outline
            doc_meta["outline_source"] = "youtube_chapters"

    job_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())
    await create_document(
        db,
        document_id,
        user_id,
        source_type="youtube",
        source_ref=video_id,
        raw_text=None,
        metadata=doc_meta,
        frontmatter=frontmatter,
        content_format="youtube",
    )
    payload = {
        "youtube_video_id": video_id,
        "metadata": youtube_meta,
        "frontmatter": frontmatter,
        "duration_s": youtube_meta.get("duration_seconds"),
    }
    if autopilot_options:
        payload["autopilot_options"] = autopilot_options
    if not autopilot_enabled:
        payload["autopilot_disabled"] = True
    job_row = await create_job_extended(
        db,
        job_id,
        user_id,
        job_type=JobType.INGEST_YOUTUBE.value,
        document_id=document_id,
        payload=payload,
    )
    if settings.use_inline_queue:
        inline_progress = {"stage": "ingesting_youtube"}
        await update_job_status(db, job_id, JobStatusEnum.PROCESSING.value, progress=inline_progress)
        try:
            result = await ingest_youtube_document(
                db,
                job_id,
                user_id,
                document_id,
                video_id,
                youtube_meta,
                frontmatter,
                youtube_meta.get("duration_seconds"),
            )
        except YouTubeCaptionsError as exc:
            await update_job_status(db, job_id, JobStatusEnum.FAILED.value, error=str(exc))
            try:
                await notify_job(db, user_id=user_id, job_id=job_id, level="error", text=f"YouTube ingestion failed: {exc}")
            except Exception:
                pass
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))
        except Exception as exc:
            await update_job_status(db, job_id, JobStatusEnum.FAILED.value, error=str(exc))
            try:
                await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="YouTube ingestion failed")
            except Exception:
                pass
            logger.exception(
                "inline_youtube_ingest_failed",
                extra={"job_id": job_id, "document_id": document_id, "error": str(exc)},
            )
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to ingest YouTube video inline.") from exc
        if isinstance(result, dict) and "job_output" in result:
            await set_job_output(db, job_id, result["job_output"])
        else:
            logger.error(
                "inline_youtube_missing_job_output",
                extra={
                    "job_id": job_id,
                    "document_id": document_id,
                    "result_type": type(result).__name__,
                    "result_keys": list(result.keys()) if isinstance(result, dict) else None,
                },
            )
            await set_job_output(db, job_id, {})

        title_hint = (
            frontmatter.get("title")
            or youtube_meta.get("title")
            or video_id
        )
        if settings.enable_drive_pipeline:
            try:
                await record_pipeline_event(
                    db,
                    user_id,
                    job_id,
                    event_type="ingest_youtube",
                    stage="drive.workspace.link",
                    status="running",
                    message="Linking document to Drive workspace",
                    data={"document_id": document_id},
                )
            except Exception:
                pass
            try:
                await link_document_drive_workspace(
                    db,
                    user_id=user_id,
                    document_id=document_id,
                    document_name=title_hint,
                    metadata=result.get("document_metadata") if isinstance(result, dict) else {},
                    job_id=job_id,
                    event_type="ingest_youtube",
                )
                try:
                    await sync_drive_doc_for_document(
                        db,
                        user_id,
                        document_id,
                        {"metadata": {"drive_stage": "transcript"}},
                    )
                except Exception as exc:
                    logger.warning(
                        "inline_drive_doc_seed_failed",
                        exc_info=True,
                        extra={"job_id": job_id, "document_id": document_id, "error": str(exc)},
                    )
                try:
                    await record_pipeline_event(
                        db,
                        user_id,
                        job_id,
                        event_type="ingest_youtube",
                        stage="drive.workspace.link",
                        status="completed",
                        message="Drive workspace linked",
                        data={"document_id": document_id},
                    )
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(
                    "inline_drive_workspace_link_failed",
                    exc_info=True,
                    extra={"job_id": job_id, "document_id": document_id, "error": str(exc)},
                )
                try:
                    await record_pipeline_event(
                        db,
                        user_id,
                        job_id,
                        event_type="ingest_youtube",
                        stage="drive.workspace.link",
                        status="error",
                        message="Drive workspace link failed",
                        data={"document_id": document_id},
                    )
                except Exception:
                    pass
        else:
            try:
                await record_pipeline_event(
                    db,
                    user_id,
                    job_id,
                    event_type="ingest_youtube",
                    stage="drive.workspace.link",
                    status="skipped",
                    message="Drive workspace linking disabled",
                    data={"document_id": document_id},
                )
            except Exception:
                pass
        await update_job_status(db, job_id, JobStatusEnum.COMPLETED.value, progress={"stage": "completed"})
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="success", text=f"Ingested YouTube {video_id}")
        except Exception:
            pass
        final_row = await get_job(db, job_id, user_id)
        if not final_row:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Job not found after inline ingestion.")
        progress = _parse_job_progress_model(final_row.get("progress", "{}"))
        output = final_row.get("output")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except Exception:
                output = None
        completed_at = final_row.get("completed_at")

        autopilot_job: Optional[JobStatus] = None
        if settings.auto_generate_after_ingest and autopilot_enabled:
            try:
                overrides = {k: v for k, v in (autopilot_options or {}).items() if v is not None}
                override_model = GenerateBlogOptions(**overrides) if overrides else GenerateBlogOptions()
                request = GenerateBlogRequest(document_id=document_id, options=override_model)
                autopilot_job = await start_generate_blog_job(db, queue, user_id, request)
                try:
                    await record_pipeline_event(
                        db,
                        user_id,
                        job_id,
                        event_type="generate_blog",
                        stage="enqueue",
                        status="running",
                        message="Starting AI draft pipeline",
                        data={"document_id": document_id, "next_job_id": autopilot_job.job_id},
                    )
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(
                    "inline_autopilot_generate_failed",
                    exc_info=True,
                    extra={"document_id": document_id, "job_id": job_id, "error": str(exc)},
                )

        return JobStatus(
            job_id=job_id,
            user_id=user_id,
            status=JobStatusEnum.COMPLETED,
            progress=progress,
            created_at=_parse_db_datetime(final_row.get("created_at")),
            completed_at=_parse_db_datetime(completed_at) if completed_at else None,
            job_type=JobType.INGEST_YOUTUBE.value,
            document_id=document_id,
            output=output,
        )
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


async def start_ingest_text_job(
    db,
    queue,
    user_id: str,
    text: str,
    title: Optional[str] = None,
    *,
    autopilot_options: Optional[Dict[str, Any]] = None,
    autopilot_enabled: bool = True,
) -> JobStatus:
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
    payload = {"title": title}
    if autopilot_options:
        payload["autopilot_options"] = autopilot_options
    if not autopilot_enabled:
        payload["autopilot_disabled"] = True
    job_row = await create_job_extended(
        db,
        job_id,
        user_id,
        job_type=JobType.INGEST_TEXT.value,
        document_id=document_id,
        payload=payload,
    )
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


@router.post("/api/v1/projects", response_model=ProjectResponse, tags=["Projects"])
async def create_project_for_youtube(req: CreateProjectRequest, user: dict = Depends(get_current_user)):
    """Create a new project backed by a YouTube document.

    Reuses the existing YouTube ingest flow to create the document and kick
    off ingestion; then creates a project record linked to that document.
    """
    db = ensure_db()
    # Reuse the existing ingest orchestration to avoid duplicating logic.
    # Disable autopilot so we don't start blog generation from this path.
    _, queue = ensure_services()
    youtube_url_str = str(req.youtube_url)
    job_status = await start_ingest_youtube_job(
        db,
        queue,
        user["user_id"],
        youtube_url_str,
        autopilot_enabled=False,
    )
    document_id = job_status.document_id
    # Create project row and mark transcript_ready if we already have text.
    try:
        project_row = await create_project(db, user["user_id"], document_id, youtube_url_str)
        doc = await get_document(db, document_id, user_id=user["user_id"])
        if doc and (doc.get("raw_text") or "").strip():
            updated = await update_project_status(
                db,
                project_row["project_id"],
                user["user_id"],
                "transcript_ready",
            )
            if updated:
                project_row["status"] = "transcript_ready"
    except DatabaseError as exc:
        logger.error(
            "create_project_for_youtube.db_error",
            exc_info=True,
            extra={"document_id": document_id, "user_id": user["user_id"], "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create project",
        ) from exc
    # Build lightweight embedded document payload
    document_payload = None
    if doc:
        metadata = _json_field(doc.get("metadata"), {})
        document_payload = {
            "document_id": doc.get("document_id"),
            "user_id": doc.get("user_id"),
            "source_type": doc.get("source_type"),
            "metadata": metadata,
            "frontmatter": _json_field(doc.get("frontmatter"), {}),
            "content_format": doc.get("content_format"),
        }
    return ProjectResponse(project=project_row, document=document_payload)


@router.get("/api/v1/projects/{project_id}", response_model=ProjectResponse, tags=["Projects"])
async def get_project_overview(project_id: str, user: dict = Depends(get_current_user)):
    db = ensure_db()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    doc = await get_document(db, project["document_id"], user_id=user["user_id"])
    document_payload = None
    if doc:
        metadata = _json_field(doc.get("metadata"), {})
        document_payload = {
            "document_id": doc.get("document_id"),
            "user_id": doc.get("user_id"),
            "source_type": doc.get("source_type"),
            "metadata": metadata,
            "frontmatter": _json_field(doc.get("frontmatter"), {}),
            "content_format": doc.get("content_format"),
        }
    return ProjectResponse(project=project, document=document_payload)


@router.post(
    "/api/v1/projects/{project_id}/transcript/chunk-and-embed",
    response_model=ChunkAndEmbedResponse,
    tags=["Projects"],
)
async def chunk_and_embed_transcript(project_id: str, user: dict = Depends(get_current_user)):
    db = ensure_db()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    doc = await get_document(db, project["document_id"], user_id=user["user_id"])
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    text = (doc.get("raw_text") or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Project document is missing transcript text")

    chunks = chunk_transcript(text)
    if not chunks:
        return ChunkAndEmbedResponse(
            project_id=project_id,
            chunks_created=0,
            embeddings_stored=0,
            status=project.get("status", "pending"),
        )

    # Persist chunks in DB. First clear any existing rows so this endpoint
    # can be safely re-run for the same project without UNIQUE constraint
    # violations on (project_id, chunk_index).
    try:
        await db.execute(
            "DELETE FROM transcript_chunks WHERE project_id = ?",
            (project_id,),
        )
    except Exception as exc:
        logger.error(
            "chunk_and_embed.delete_transcript_chunks_failed",
            exc_info=True,
            extra={"project_id": project_id, "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset existing transcript chunks",
        ) from exc

    persisted_chunks = []
    had_chunk_errors = False
    for chunk in chunks:
        try:
            # Validate required numeric fields before casting.
            if "chunk_index" not in chunk or "start_char" not in chunk or "end_char" not in chunk:
                raise ValueError("chunk_index, start_char, and end_char are required for each chunk")
            chunk_index = int(chunk["chunk_index"])
            start_char = int(chunk["start_char"])
            end_char = int(chunk["end_char"])
            await create_transcript_chunk(
                db,
                chunk_id=str(uuid.uuid4()),
                project_id=project_id,
                document_id=project["document_id"],
                chunk_index=chunk_index,
                start_char=start_char,
                end_char=end_char,
                text_preview=(chunk.get("text") or "")[:200],
            )
            persisted_chunks.append({
                "chunk_index": chunk_index,
                "start_char": start_char,
                "end_char": end_char,
                "text": chunk.get("text") or "",
            })
        except (ValueError, TypeError) as exc:
            had_chunk_errors = True
            logger.warning(
                "chunk_and_embed.invalid_chunk",
                extra={
                    "project_id": project_id,
                    "document_id": project["document_id"],
                    "chunk": chunk,
                    "error": str(exc),
                },
            )
            continue
        except Exception as exc:
            had_chunk_errors = True
            logger.error(
                "chunk_and_embed.create_transcript_chunk_failed",
                exc_info=True,
                extra={
                    "project_id": project_id,
                    "document_id": project["document_id"],
                    "chunk": chunk,
                    "error": str(exc),
                },
            )
            continue

    if not persisted_chunks:
        # No chunks could be safely saved; mark project as failed and abort.
        try:
            await update_project_status(db, project_id, user["user_id"], "failed")
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist any transcript chunks for project",
        )

    if had_chunk_errors:
        # Some chunks failed to save; treat as a hard failure rather than
        # proceeding with a partial embedding.
        try:
            await update_project_status(db, project_id, user["user_id"], "failed")
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="One or more transcript chunks failed to save",
        )

    # Build embeddings payloads from successfully persisted chunks only
    texts = [c["text"] for c in persisted_chunks]
    try:
        vectors = await embed_texts(texts)
    except NotImplementedError as exc:
        # Surface a clear error until embeddings are wired
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "chunk_and_embed.embed_texts_failed",
            exc_info=True,
            extra={
                "project_id": project_id,
                "document_id": project["document_id"],
                "chunks": len(persisted_chunks),
                "error": str(exc),
            },
        )
        try:
            await update_project_status(db, project_id, user["user_id"], "failed")
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embeddings service unavailable for transcript chunks",
        ) from exc

    metadatas = [
        {
            "project_id": project_id,
            "document_id": project["document_id"],
            "chunk_index": c["chunk_index"],
            "start_char": c["start_char"],
            "end_char": c["end_char"],
        }
        for c in persisted_chunks
    ]

    try:
        embeddings_stored = await store_embeddings(vectors=vectors, metadatas=metadatas)
    except Exception as exc:
        logger.error(
            "chunk_and_embed.store_embeddings_failed",
            exc_info=True,
            extra={
                "project_id": project_id,
                "document_id": project["document_id"],
                "chunks": len(chunks),
                "error": str(exc),
            },
        )
        # Best-effort mark project as failed; ignore errors from status update.
        try:
            await update_project_status(db, project_id, user["user_id"], "failed")
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to store embeddings for transcript chunks",
        ) from exc

    try:
        updated = await update_project_status(db, project_id, user["user_id"], "embedded")
    except DatabaseError as exc:
        logger.error(
            "chunk_and_embed.update_project_status_failed",
            exc_info=True,
            extra={"project_id": project_id, "user_id": user["user_id"], "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Embeddings stored, but failed to update project status",
        ) from exc

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found or not owned by user",
        )

    return ChunkAndEmbedResponse(
        project_id=project_id,
        chunks_created=len(chunks),
        embeddings_stored=embeddings_stored,
        status="embedded",
    )


@router.get(
    "/api/v1/projects/{project_id}/transcript",
    response_model=TranscriptResponse,
    tags=["Projects"],
)
async def get_project_transcript(project_id: str, user: dict = Depends(get_current_user)):
    db = ensure_db()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    doc = await get_document(db, project["document_id"], user_id=user["user_id"])
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    text = (doc.get("raw_text") or "").strip()
    chunks = await list_transcript_chunks(db, project_id, user["user_id"])
    return TranscriptResponse(
        project_id=project_id,
        text=text,
        chunks=chunks,
        metadata={"document_id": project["document_id"]},
    )


@router.post(
    "/api/v1/projects/{project_id}/transcript/search",
    response_model=TranscriptSearchResponse,
    tags=["Projects"],
)
async def search_project_transcript(
    project_id: str,
    req: TranscriptSearchRequest,
    user: dict = Depends(get_current_user),
):
    db = ensure_db()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # Embed the query text
    try:
        vectors = await embed_texts([req.query])
    except NotImplementedError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc

    if not vectors or not isinstance(vectors[0], list):
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Embedding failed for query")

    query_vec = vectors[0]

    # Query Vectorize for best-matching chunks. Errors in the vector service
    # are surfaced as 502/503 rather than generic 500s.
    try:
        hits = await query_project_chunks(
            project_id=project_id,
            query_vector=query_vec,
            limit=req.limit,
        )
    except Exception as exc:
        logger.error(
            "transcript_search.vectorize_query_failed",
            exc_info=True,
            extra={
                "project_id": project_id,
                "query_length": len(req.query or ""),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Vector search failed for this project",
        ) from exc

    # Load known chunks for this project so we can enrich results with
    # preview/start/end even if metadata is incomplete.
    existing_chunks = await list_transcript_chunks(db, project_id, user["user_id"])
    by_index = {int(c["chunk_index"]): c for c in existing_chunks}

    matches_payload = []
    for hit in hits:
        meta = hit.get("metadata") or {}
        hit_id = str(hit.get("id") or "")

        # Restrict hits to this project using metadata when present
        meta_project_id = meta.get("project_id")
        if meta_project_id is not None and meta_project_id != project_id:
            continue

        idx = meta.get("chunk_index")
        if idx is None:
            # Fallback: id is typically "project_id:document_id:chunk_index"
            if not hit_id.startswith(f"{project_id}:"):
                continue
            try:
                _, _, idx_str = hit_id.rsplit(":", 2)
                idx = int(idx_str)
            except (TypeError, ValueError):
                continue

        try:
            idx_int = int(idx)
        except (TypeError, ValueError):
            continue
        chunk_row = by_index.get(idx_int)
        if not chunk_row:
            continue
        matches_payload.append(
            {
                "chunk_id": chunk_row.get("chunk_id"),
                "chunk_index": idx_int,
                "start_char": chunk_row.get("start_char"),
                "end_char": chunk_row.get("end_char"),
                "text_preview": chunk_row.get("text_preview") or "",
                "score": hit.get("score"),
            }
        )

    return TranscriptSearchResponse(
        project_id=project_id,
        query=req.query,
        matches=matches_payload,
    )


@router.post(
    "/api/v1/projects/{project_id}/blog/generate",
    response_model=GenerateProjectBlogResponse,
    tags=["Projects"],
)
async def generate_project_blog(
    project_id: str,
    req: ProjectGenerateBlogRequest,
    user: dict = Depends(get_current_user),
):
    db, queue = ensure_services()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    doc = await get_document(db, project["document_id"], user_id=user["user_id"])
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    text = (doc.get("raw_text") or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project document is missing transcript text to generate from",
        )

    user_prefs = await get_user_preferences(db, user["user_id"])
    options_model = req.options or GenerateBlogOptions()
    resolved_options = resolve_generate_blog_options(options_model, user_prefs)

    job_id = str(uuid.uuid4())
    await create_job_extended(
        db,
        job_id,
        user["user_id"],
        job_type=JobType.GENERATE_BLOG.value,
        document_id=project["document_id"],
        payload={"options": resolved_options, "project_id": project_id},
    )

    # Inline mode: run the blog job synchronously, mirroring start_generate_blog_job.
    if settings.use_inline_queue:
        logger.info(
            "generate_project_blog.inline_start",
            extra={
                "job_id": job_id,
                "project_id": project_id,
                "document_id": project["document_id"],
                "user_id": user["user_id"],
            },
        )
        try:
            await process_generate_blog_job(
                db,
                job_id,
                user["user_id"],
                project["document_id"],
                options=resolved_options,
            )
        except HTTPException:
            logger.exception(
                "generate_project_blog.inline_http_error",
                extra={
                    "job_id": job_id,
                    "project_id": project_id,
                    "document_id": project["document_id"],
                    "user_id": user["user_id"],
                },
            )
            raise
        except Exception as exc:
            logger.exception(
                "generate_project_blog.inline_failed",
                extra={
                    "job_id": job_id,
                    "project_id": project_id,
                    "document_id": project["document_id"],
                    "user_id": user["user_id"],
                    "error": str(exc),
                },
            )
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate blog inline for project.",
            ) from exc

        final_row = await get_job(db, job_id, user["user_id"])
        if not final_row:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Job not found after inline project blog generation.",
            )
        logger.info(
            "generate_project_blog.inline_complete",
            extra={
                "job_id": job_id,
                "project_id": project_id,
                "document_id": project["document_id"],
                "user_id": user["user_id"],
                "status": final_row.get("status"),
            },
        )

        # In inline mode we also advance the project status so dev flows
        # behave like background-consumer flows, but only when the job
        # actually completed successfully.
        final_status = (final_row.get("status") or "").lower()
        if final_status in {JobStatusEnum.COMPLETED.value, "completed"}:
            try:
                updated = await update_project_status(db, project_id, user["user_id"], "blog_generated")
                if updated:
                    project["status"] = "blog_generated"
            except Exception:
                logger.warning(
                    "generate_project_blog.inline_status_update_failed",
                    exc_info=True,
                    extra={"project_id": project_id, "user_id": user["user_id"], "job_id": job_id},
                )
        else:
            logger.warning(
                "generate_project_blog.inline_status_not_completed",
                extra={
                    "project_id": project_id,
                    "user_id": user["user_id"],
                    "job_id": job_id,
                    "final_status": final_row.get("status"),
                },
            )

        return GenerateProjectBlogResponse(
            job_id=job_id,
            blog=None,
            project=project,
        )

    # Non-inline mode: enqueue for background consumer processing.
    payload = {
        "job_id": job_id,
        "user_id": user["user_id"],
        "job_type": JobType.GENERATE_BLOG.value,
        "document_id": project["document_id"],
        "options": resolved_options,
        "project_id": project_id,
    }
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(
        queue,
        job_id,
        user["user_id"],
        payload,
        allow_inline_fallback=False,
    )
    if should_fail:
        detail = "Queue unavailable or enqueue failed; background processing is required in production."
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    if not enqueued:
        logger.warning(
            "generate_project_blog_job_enqueued_false",
            extra={
                "job_id": job_id,
                "user_id": user["user_id"],
                "project_id": project_id,
                "event": "job.enqueue_failed",
                "reason": "queue unavailable or enqueue failed",
                "enqueue_exception": (str(enqueue_exception) if enqueue_exception else None),
            },
        )

    return GenerateProjectBlogResponse(
        job_id=job_id,
        blog=None,
        project=project,
    )


@router.get(
    "/api/v1/projects/{project_id}/blog",
    response_model=ProjectBlog,
    tags=["Projects"],
)
async def get_project_blog(project_id: str, user: dict = Depends(get_current_user)):
    db = ensure_db()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    doc = await get_document(db, project["document_id"], user_id=user["user_id"])
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    latest_version_id = doc.get("latest_version_id")
    if not latest_version_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No blog generated for this project yet")

    version_row = await get_document_version(db, doc["document_id"], latest_version_id, user["user_id"])
    if not version_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blog version not found")

    payload = _version_payload(version_row)
    return ProjectBlog(
        project_id=project_id,
        document_id=payload["document_id"],
        version_id=payload["version_id"],
        status=project.get("status"),
        frontmatter=payload["frontmatter"],
        body_mdx=payload["body_mdx"],
        outline=payload["outline"],
        created_at=payload["created_at"],
    )


@router.get(
    "/api/v1/projects/{project_id}/blog/sections",
    response_model=ProjectSectionListResponse,
    tags=["Projects"],
)
async def list_project_blog_sections(
    project_id: str,
    user: dict = Depends(get_current_user),
):
    db = ensure_db()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    version_row = await get_latest_version_for_project(db, project, user["user_id"])
    if not version_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No blog generated for this project yet")

    sections = extract_sections_from_version(version_row)
    summaries: List[ProjectSectionSummary] = []
    for section in sections:
        section_id = str(section.get("section_id"))
        index = int(section.get("index", 0))
        title = section.get("title")
        # For now, derive word_count from summary/title as a cheap approximation.
        text_for_wc = section.get("summary") or title or ""
        wc = _word_count(text_for_wc)
        summaries.append(
            ProjectSectionSummary(
                section_id=section_id,
                index=index,
                title=title,
                word_count=wc,
            )
        )

    return ProjectSectionListResponse(
        project_id=project_id,
        document_id=version_row.get("document_id"),
        version_id=version_row.get("version_id"),
        sections=summaries,
    )


@router.get(
    "/api/v1/projects/{project_id}/blog/versions",
    response_model=ProjectVersionsResponse,
    tags=["Projects"],
)
async def list_project_blog_versions(
    project_id: str,
    user: dict = Depends(get_current_user),
):
    db = ensure_db()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    document_id = project.get("document_id")
    if not document_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project is missing document reference")

    rows = await list_document_versions(db, document_id, user["user_id"], limit=50)
    versions: List[ProjectVersionSummary] = []
    for row in rows:
        created_at = _parse_db_datetime(row.get("created_at"))
        frontmatter = _json_field(row.get("frontmatter"), {})
        title = None
        if isinstance(frontmatter, dict):
            title = frontmatter.get("title")
        # Best-effort source hint from assets.generator.source
        assets = _json_field(row.get("assets"), {})
        source = None
        if isinstance(assets, dict):
            gen = assets.get("generator") or {}
            if isinstance(gen, dict):
                source = gen.get("source")
        versions.append(
            ProjectVersionSummary(
                version_id=row.get("version_id"),
                version=int(row.get("version", 0)),
                created_at=created_at,
                source=source,
                title=title,
            )
        )

    return ProjectVersionsResponse(
        project_id=project_id,
        document_id=document_id,
        versions=versions,
    )


@router.get(
    "/api/v1/projects/{project_id}/blog/versions/{version_id}",
    response_model=ProjectVersionDetail,
    tags=["Projects"],
)
async def get_project_blog_version(
    project_id: str,
    version_id: str,
    user: dict = Depends(get_current_user),
):
    db = ensure_db()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    document_id = project.get("document_id")
    if not document_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project is missing document reference")

    row = await get_document_version(db, document_id, version_id, user["user_id"])
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blog version not found")

    frontmatter = _json_field(row.get("frontmatter"), {})
    outline = _json_field(row.get("outline"), [])
    sections = _json_field(row.get("sections"), [])

    return ProjectVersionDetail(
        project_id=project_id,
        document_id=document_id,
        version_id=row.get("version_id"),
        version=int(row.get("version", 0)),
        created_at=_parse_db_datetime(row.get("created_at")),
        frontmatter=frontmatter,
        body_mdx=row.get("body_mdx"),
        outline=outline,
        sections=sections,
    )


@router.get(
    "/api/v1/projects/{project_id}/blog/export",
    tags=["Projects"],
)
async def export_project_blog_mdx(
    project_id: str,
    user: dict = Depends(get_current_user),
):
    db = ensure_db()
    project = await get_project(db, project_id, user["user_id"])
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    version_row = await get_latest_version_for_project(db, project, user["user_id"])
    if not version_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No blog generated for this project yet")

    body_mdx = version_row.get("body_mdx") or ""
    return {"project_id": project_id, "document_id": version_row.get("document_id"), "version_id": version_row.get("version_id"), "body_mdx": body_mdx}


async def start_generate_blog_job(
    db,
    queue,
    user_id: str,
    req: GenerateBlogRequest,
) -> JobStatus:
    document_id = req.document_id
    logger.info(
        "start_generate_blog_job.begin",
        extra={
            "document_id": document_id,
            "user_id": user_id,
            "inline_mode": settings.use_inline_queue,
        },
    )
    doc = await _load_document_for_user(db, document_id, user_id)
    text = (doc.get("raw_text") or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Document is missing raw text to generate from")
    user_prefs = await get_user_preferences(db, user_id)
    resolved_options = resolve_generate_blog_options(req.options, user_prefs)
    job_id = str(uuid.uuid4())
    job_row = await create_job_extended(
        db,
        job_id,
        user_id,
        job_type=JobType.GENERATE_BLOG.value,
        document_id=document_id,
        payload={"options": resolved_options},
    )

    # In inline mode we run the full blog generation pipeline synchronously,
    # mirroring the behavior of inline YouTube ingest so that dev runs can
    # produce completed jobs and versions without a background consumer.
    if settings.use_inline_queue:
        logger.info(
            "start_generate_blog_job.inline_start",
            extra={
                "job_id": job_id,
                "document_id": document_id,
                "user_id": user_id,
            },
        )
        try:
            await process_generate_blog_job(
                db,
                job_id,
                user_id,
                document_id,
                options=resolved_options,
            )
        except HTTPException:
            logger.exception(
                "start_generate_blog_job.inline_http_error",
                extra={
                    "job_id": job_id,
                    "document_id": document_id,
                    "user_id": user_id,
                },
            )
            raise
        except Exception as exc:
            logger.exception(
                "inline_generate_blog_failed",
                extra={
                    "job_id": job_id,
                    "document_id": document_id,
                    "user_id": user_id,
                    "error": str(exc),
                },
            )
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate blog inline.",
            ) from exc

        final_row = await get_job(db, job_id, user_id)
        if not final_row:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Job not found after inline blog generation.",
            )
        logger.info(
            "start_generate_blog_job.inline_complete",
            extra={
                "job_id": job_id,
                "document_id": document_id,
                "user_id": user_id,
                "status": final_row.get("status"),
            },
        )
        progress = _parse_job_progress_model(final_row.get("progress", "{}"))
        completed_at = final_row.get("completed_at")
        output = final_row.get("output")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except Exception:
                output = None

        status_raw = final_row.get("status") or JobStatusEnum.COMPLETED.value
        try:
            status_enum = JobStatusEnum(status_raw)
        except ValueError:
            status_enum = JobStatusEnum.COMPLETED

        return JobStatus(
            job_id=job_id,
            user_id=user_id,
            status=status_enum,
            progress=progress,
            created_at=_parse_db_datetime(final_row.get("created_at")),
            completed_at=_parse_db_datetime(completed_at) if completed_at else None,
            job_type=JobType.GENERATE_BLOG.value,
            document_id=document_id,
            output=output,
        )

    payload = {
        "job_id": job_id,
        "user_id": user_id,
        "job_type": JobType.GENERATE_BLOG.value,
        "document_id": document_id,
        "options": resolved_options,
    }
    enqueued, enqueue_exception, should_fail = await enqueue_job_with_guard(
        queue,
        job_id,
        user_id,
        payload,
        allow_inline_fallback=False,
    )
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


@router.get("/debug/google", tags=["Debug"])
async def debug_google_integrations(
    request: Request,
    video_id: str = Query("p12N2v2WHDA", description="YouTube video ID to test"),
    user: dict = Depends(get_current_user),
):
    """Debug endpoint to exercise YouTube and Drive integrations from Workers.

    - Verifies that OAuth tokens can be loaded for the current user.
    - Performs lightweight YouTube and Drive API calls.
    - Logs detailed success/failure information for Cloudflare observability.
    """

    db = ensure_db()
    user_id = user.get("user_id")

    results: Dict[str, Any] = {
        "user_id": user_id,
        "video_id": video_id,
        "youtube_ok": False,
        "drive_ok": False,
    }

    # YouTube diagnostics
    try:
        logger.info(
            "debug_youtube_start",
            extra={"user_id": user_id, "video_id": video_id},
        )
        youtube_service = await build_youtube_service_for_user(db, user_id)
        metadata_bundle = await fetch_video_metadata_async(
            youtube_service, video_id
        )
        youtube_meta = metadata_bundle.get("metadata") or {}
        results["youtube_ok"] = True
        results["youtube_duration"] = youtube_meta.get("duration_seconds")
        results["youtube_title"] = youtube_meta.get("title")
        logger.info(
            "debug_youtube_success",
            extra={
                "user_id": user_id,
                "video_id": video_id,
                "duration": youtube_meta.get("duration_seconds"),
                "title": youtube_meta.get("title"),
            },
        )
    except Exception as exc:
        results["youtube_error"] = str(exc)
        logger.error(
            "debug_youtube_error",
            exc_info=True,
            extra={"user_id": user_id, "video_id": video_id, "error": str(exc)},
        )

    # Drive diagnostics (minimal list of files from root folder)
    try:
        logger.info(
            "debug_drive_start",
            extra={"user_id": user_id},
        )
        drive_service = await build_drive_service_for_user(db, user_id)
        # Use async path in Workers runtime when available
        if hasattr(drive_service, "list_folder_files_async"):
            drive_listing = await drive_service.list_folder_files_async("root")  # type: ignore[attr-defined]
        else:
            drive_listing = await asyncio.to_thread(
                drive_service.list_folder_files,
                "root",
            )
        files = drive_listing.get("files") or []
        results["drive_ok"] = True
        results["drive_file_count"] = len(files)
        logger.info(
            "debug_drive_success",
            extra={
                "user_id": user_id,
                "file_count": len(files),
            },
        )
    except Exception as exc:
        results["drive_error"] = str(exc)
        logger.error(
            "debug_drive_error",
            exc_info=True,
            extra={"user_id": user_id, "error": str(exc)},
        )

    logger.info(
        "debug_google_integrations_complete",
        extra={"user_id": user_id, "video_id": video_id, "results": results},
    )

    return results


@router.get("/api/v1/debug/env", tags=["Debug"])
async def debug_env():
    return {
        "environment": settings.environment,
        "use_inline_queue": settings.use_inline_queue,
        "queue_bound": settings.queue is not None,
        "dlq_bound": settings.dlq is not None,
        "enable_notifications": getattr(settings, "enable_notifications", False),
        "openai_config": {
            "api_key_set": bool(getattr(settings, "openai_api_key", None)),
            "api_base": getattr(settings, "openai_api_base", None),
            "blog_model": getattr(settings, "openai_blog_model", None),
            "blog_temperature": getattr(settings, "openai_blog_temperature", None),
            "blog_max_output_tokens": getattr(settings, "openai_blog_max_output_tokens", None),
        },
        "ai_gateway_config": {
            "cloudflare_account_id": getattr(settings, "cloudflare_account_id", None),
            "token_set": bool(getattr(settings, "cf_ai_gateway_token", None)),
            "openai_api_base": getattr(settings, "openai_api_base", None),
        },
    }


@router.get("/api/v1/debug/ai-gateway-test", tags=["Debug"])
async def debug_ai_gateway_test():
    """Exercise Cloudflare AI Gateway chat completions using current Worker configuration.

    This endpoint is intended for local/staging verification only and is
    exposed via the Settings debug card. It performs a single chat.completions
    call against the configured AI Gateway using the compat endpoint.
    """

    if not settings.cloudflare_account_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CLOUDFLARE_ACCOUNT_ID is not configured",
        )
    if not getattr(settings, "cf_ai_gateway_token", None):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CF_AI_GATEWAY_TOKEN is not configured",
        )

    gateway_base = "https://gateway.ai.cloudflare.com"
    endpoint_path = f"/v1/{settings.cloudflare_account_id}/quill/compat/chat/completions"

    prompt = "Test request from Quill via Cloudflare AI Gateway. Respond with a short confirmation message."
    model_name = "openai/gpt-4.1-mini"

    client = AsyncSimpleClient(base_url=gateway_base, timeout=20.0)

    logger.info(
        "debug_ai_gateway_test_request",
        extra={
            "gateway_base": gateway_base,
            "endpoint_path": endpoint_path,
            "model": model_name,
        },
    )

    started_at = datetime.now(timezone.utc)
    try:
        response = await client.post(
            endpoint_path,
            headers={
                "Content-Type": "application/json",
                "cf-aig-authorization": f"Bearer {settings.cf_ai_gateway_token}",
            },
            json={
                "model": model_name,
                "messages": [
                    {"role": "user", "content": prompt},
                ],
            },
        )
        response.raise_for_status()
    except HTTPStatusError as exc:
        logger.error(
            "debug_ai_gateway_test_http_error",
            exc_info=True,
            extra={
                "status_code": exc.response.status_code,
                "body": _redact_http_body_for_logging(getattr(exc.response, "text", None)),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI Gateway returned HTTP {exc.response.status_code}",
        ) from exc
    except RequestError as exc:
        logger.error(
            "debug_ai_gateway_test_request_error",
            exc_info=True,
            extra={"error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to reach AI Gateway",
        ) from exc
    except Exception as exc:
        logger.error(
            "debug_ai_gateway_test_unexpected_error",
            exc_info=True,
            extra={"error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error calling AI Gateway",
        ) from exc

    data: Dict[str, Any]
    try:
        data = response.json()
    except Exception:
        logger.error(
            "debug_ai_gateway_test_invalid_json",
            extra={"body_preview": _redact_http_body_for_logging(getattr(response, "text", None))},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI Gateway returned invalid JSON",
        )

    reply_text: Optional[str] = None
    try:
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            reply_text = message.get("content")
    except Exception:
        reply_text = None

    # Derive basic diagnostics for easier debugging
    status_code = getattr(response, "status_code", None)
    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    raw_body_preview = _redact_http_body_for_logging(getattr(response, "text", None))

    logger.info(
        "debug_ai_gateway_test_response",
        extra={
            "model": model_name,
            "has_reply": bool(reply_text),
            "usage": data.get("usage"),
            "status_code": status_code,
            "duration_ms": duration_ms,
        },
    )

    return {
        "gateway_base": gateway_base,
        "endpoint_path": endpoint_path,
        "model": model_name,
        "prompt": prompt,
        "reply_preview": (reply_text or "")[:500],
        "raw_usage": data.get("usage"),
        "status_code": status_code,
        "duration_ms": duration_ms,
        "raw_response_preview": raw_body_preview,
    }


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
async def google_auth_start(
    request: Request,
    integration: str = Query("drive", description="Google integration to connect (drive, youtube, gmail)"),
    redirect: Optional[str] = Query(None, description="Optional path to redirect after linking"),
    user: dict = Depends(get_current_user),
):
    """Start Google OAuth flow for linking an integration.
    
    Stores OAuth state in user's session instead of cookies for better cross-site redirect reliability.
    """
    from .database import touch_user_session
    from .flash import add_flash
    import json
    
    # Check OAuth configuration early to provide better error message
    if not settings.google_client_id or not settings.google_client_secret:
        # Redirect back to integrations page with flash error (for browser requests)
        integration_key = normalize_google_integration(integration)
        redirect_path = redirect or f"/dashboard/integrations/{integration_key}"
        redirect_path = _validate_redirect_path(redirect_path, "/dashboard/integrations")
        # Try to add flash message, but don't fail if session doesn't exist
        try:
            await add_flash(request, "Google OAuth is not configured. Please contact support.", category="error")
        except Exception as flash_error:
            logger.debug(f"Could not add flash message (session may not exist): {flash_error}")
        return RedirectResponse(url=redirect_path, status_code=status.HTTP_302_FOUND)
    
    try:
        integration_key = normalize_google_integration(integration)
        if settings.base_url:
            redirect_uri = f"{settings.base_url.rstrip('/')}/auth/google/callback"
        else:
            redirect_uri = str(request.url.replace(path="/auth/google/callback", query=""))
        state = secrets.token_urlsafe(16)
        auth_url = get_google_oauth_url(state, redirect_uri, integration=integration_key)

        # Store OAuth state in user's session instead of cookies
        # This is more reliable for cross-site redirects
        db = ensure_db()
        session = getattr(request.state, "session", None)
        session_id = getattr(request.state, "session_id", None)
        
        logger.debug(
            "Google integration OAuth start: session_present=%s, session_id=%s, user_id=%s",
            session is not None,
            session_id,
            user["user_id"],
        )
        
        # If no session exists, create one for this authenticated user
        if not session_id:
            from .database import create_user_session, create_user, get_user_by_id
            from datetime import timedelta
            # Ensure user exists in database (required for foreign key constraint in user_sessions)
            # Check if user exists first to avoid UNIQUE constraint violations on github_id/google_id
            existing_user = await get_user_by_id(db, user["user_id"])
            if not existing_user:
                # Only create if user doesn't exist
                # create_user handles UNIQUE constraint violations gracefully by returning existing user
                try:
                    created_user = await create_user(
                        db,
                        user["user_id"],
                        github_id=user.get("github_id"),
                        google_id=user.get("google_id"),
                        email=user.get("email"),
                    )
                    # Use the returned user (might be different if UNIQUE constraint returned existing user)
                    existing_user = created_user
                except Exception as create_error:
                    # If create fails, try to get the existing user - they might have been created by another request
                    logger.warning(f"create_user failed, checking if user exists: {create_error}")
                    existing_user = await get_user_by_id(db, user["user_id"])
                    if not existing_user:
                        # If user still doesn't exist, we can't create a session - this is a critical error
                        logger.error(f"Cannot create session: user {user['user_id']} does not exist and could not be created: {create_error}")
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to create user account. Please try again."
                        ) from create_error
            # Use the existing user's user_id (might be different if UNIQUE constraint returned different user)
            actual_user_id = existing_user.get("user_id") or user["user_id"]
            # Verify user exists before creating session
            if not actual_user_id:
                logger.error(f"Cannot create session: user_id is None for user {user['user_id']}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="User account error. Please try again."
                )
            session_id = secrets.token_urlsafe(32)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
            await create_user_session(
                db,
                session_id,
                actual_user_id,
                expires_at,
                ip_address=(request.client.host if request.client else None),
                user_agent=request.headers.get("user-agent"),
                extra={"oauth_state": state, "google_redirect_uri": redirect_uri, "google_integration": integration_key},
            )
            redirect_path = redirect or f"/dashboard/integrations/{integration_key}"
            redirect_path = _validate_redirect_path(redirect_path, f"/dashboard/integrations/{integration_key}")
            # Update with redirect path
            from .database import touch_user_session
            await touch_user_session(
                db,
                session_id,
                extra={"oauth_state": state, "google_redirect_uri": redirect_uri, "google_integration": integration_key, "google_redirect_next": redirect_path},
            )
            logger.info("Google integration OAuth: Created session %s and stored state for user %s", session_id, user["user_id"])
            
            # Set session cookie in response
            is_secure = settings.environment == "production" or request.url.scheme == "https"
            response = RedirectResponse(url=auth_url)
            response.set_cookie(
                key=settings.session_cookie_name,
                value=session_id,
                httponly=True,
                secure=is_secure,
                samesite="lax",
                max_age=int(settings.session_ttl_hours * 3600),
                path="/",
            )
            return response
        else:
            # Update session extra with OAuth state
            current_extra = json.loads(session.get("extra", "{}")) if isinstance(session.get("extra"), str) else (session.get("extra") or {})
            current_extra["oauth_state"] = state
            current_extra["google_redirect_uri"] = redirect_uri
            current_extra["google_integration"] = integration_key
            redirect_path = redirect or f"/dashboard/integrations/{integration_key}"
            redirect_path = _validate_redirect_path(redirect_path, f"/dashboard/integrations/{integration_key}")
            current_extra["google_redirect_next"] = redirect_path
            
            await touch_user_session(db, session_id, extra=current_extra)
            logger.info("Google integration OAuth: Stored state in session %s for user %s", session_id, user["user_id"])
        
        # If we stored in session, just redirect
        response = RedirectResponse(url=auth_url)
        return response
    except ValueError as e:
        # ValueError from get_google_oauth_url when OAuth is not configured
        if "Google OAuth not configured" in str(e):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google OAuth is not configured. Please contact support."
            ) from e
        raise
    except DatabaseError as e:
        # Database errors (including UNIQUE constraint violations)
        logger.error(f"Google auth initiation failed (database error): {e}", exc_info=True)
        # Check if it's a UNIQUE constraint violation
        from .database import _is_unique_constraint_violation
        if _is_unique_constraint_violation(e):
            # Try to redirect with error message instead of 500
            integration_key = normalize_google_integration(integration)
            redirect_path = redirect or f"/dashboard/integrations/{integration_key}"
            redirect_path = _validate_redirect_path(redirect_path, "/dashboard/integrations")
            try:
                await add_flash(request, "An error occurred while connecting. Please try again.", category="error")
            except Exception:
                pass
            return RedirectResponse(url=redirect_path, status_code=status.HTTP_302_FOUND)
        # Re-raise other database errors
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error occurred. Please try again later."
        ) from e
    except Exception as e:
        logger.error(f"Google auth initiation failed: {e}", exc_info=True)
        # Don't assume it's an OAuth configuration issue - show the actual error
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred: {str(e)}"
        ) from e


@router.get("/auth/google/callback", tags=["Authentication"])
async def google_auth_callback(code: str, state: str, request: Request, user: dict = Depends(get_current_user)):
    """Handle Google OAuth callback for integration linking.
    
    Retrieves OAuth state from user's session (preferred) or cookies (fallback).
    """
    from .database import touch_user_session
    import json
    
    db = ensure_db()

    # Try to get state from session first (preferred method)
    stored_state = None
    redirect_uri = None
    integration_key = None
    next_path = None
    
    session = getattr(request.state, "session", None)
    session_id = getattr(request.state, "session_id", None)
    
    # If session isn't loaded by middleware, try to load it manually from cookie
    # This can happen on cross-site redirects where middleware might not have loaded it
    # or when session is not in cache (middleware skips DB lookup to avoid ASGI errors)
    if not session or not session_id:
        session_cookie = request.cookies.get(settings.session_cookie_name)
        if session_cookie:
            from .database import get_user_session
            try:
                loaded_session = await get_user_session(db, session_cookie)
                if loaded_session:
                    session = loaded_session
                    session_id = session_cookie
                    logger.debug("Google integration callback: Manually loaded session %s from cookie", session_id)
                else:
                    logger.debug("Google integration callback: Session cookie %s not found in database", session_cookie)
            except Exception as exc:
                logger.warning("Google integration callback: Failed to manually load session: %s", exc, exc_info=True)
    
    logger.debug(
        "Google integration callback: session_present=%s, session_id=%s, cookies=%s",
        session is not None,
        session_id,
        list(request.cookies.keys()),
    )
    
    if session and session_id:
        session_extra = session.get("extra")
        if session_extra:
            if isinstance(session_extra, str):
                try:
                    session_extra = json.loads(session_extra)
                except Exception:
                    session_extra = {}
            else:
                session_extra = session_extra or {}
            
            stored_state = session_extra.get("oauth_state")
            redirect_uri = session_extra.get("google_redirect_uri")
            integration_key = session_extra.get("google_integration")
            next_path = session_extra.get("google_redirect_next")
            
            logger.debug(
                "Google integration callback: Found in session - state_present=%s, integration=%s",
                stored_state is not None,
                integration_key,
            )
            
            # Clean up OAuth state from session after retrieving
            if stored_state:
                session_extra.pop("oauth_state", None)
                session_extra.pop("google_redirect_uri", None)
                session_extra.pop("google_integration", None)
                session_extra.pop("google_redirect_next", None)
                await touch_user_session(db, session_id, extra=session_extra)
                logger.info("Google integration OAuth: Retrieved state from session %s", session_id)
    
    # Fallback to cookies if not found in session
    if not stored_state:
        stored_state = request.cookies.get(COOKIE_GOOGLE_OAUTH_STATE)
        if not redirect_uri:
            redirect_uri = request.cookies.get("google_redirect_uri")
        if not integration_key:
            integration_cookie = request.cookies.get("google_integration")
            if integration_cookie:
                try:
                    integration_key = normalize_google_integration(integration_cookie)
                except Exception:
                    pass
        if not next_path:
            next_path = request.cookies.get("google_redirect_next")
        
        logger.debug(
            "Google integration callback: Fallback to cookies - state_present=%s, integration=%s",
            stored_state is not None,
            integration_key,
        )

    # Verify state
    if not stored_state:
        logger.warning(
            "Google OAuth state verification failed - no stored state found. session_id=%s, cookies=%s",
            session_id,
            list(request.cookies.keys()),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid state parameter - possible CSRF attack")
    
    if not secrets.compare_digest(stored_state, state):
        logger.warning(
            "Google OAuth state verification failed - state mismatch. stored_length=%d, received_length=%d",
            len(stored_state) if stored_state else 0,
            len(state) if state else 0,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid state parameter - possible CSRF attack")

    # Validate integration
    if not redirect_uri:
        redirect_uri = str(request.url.replace(query=""))
    if not integration_key:
        try:
            integration_key = normalize_google_integration(None)  # Will use default
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing or invalid Google integration selection")

    try:
        await exchange_google_code(db, user["user_id"], code, redirect_uri, integration=integration_key)
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        if not next_path:
            next_path = f"/dashboard/integrations/{integration_key}"
        next_path = _validate_redirect_path(next_path, f"/dashboard/integrations/{integration_key}")
        response = RedirectResponse(url=next_path, status_code=status.HTTP_302_FOUND)
        
        # Clean up cookies (in case fallback was used)
        response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
        response.delete_cookie(key="google_redirect_uri", path="/", samesite="lax", httponly=True, secure=is_secure)
        response.delete_cookie(key="google_integration", path="/", samesite="lax", httponly=True, secure=is_secure)
        response.delete_cookie(key="google_redirect_next", path="/", samesite="lax", httponly=True, secure=is_secure)
        return response
    except Exception as e:
        logger.error(f"Google callback failed: {e}", exc_info=True)
        error_detail = str(e) if settings.debug else "Google authentication failed"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error_detail) from None


@router.get("/auth/google/status", tags=["Authentication"])
async def google_link_status(user: dict = Depends(get_current_user)):
    db = ensure_db()
    rows = await list_google_tokens(db, user["user_id"])  # type: ignore
    summary = _summarize_google_tokens(rows)
    return {
        "linked": bool(summary),
        "integrations": summary,
    }


@router.get("/auth/providers/status", tags=["Authentication"])
async def providers_status(user: dict = Depends(get_current_user)):
    db = ensure_db()
    # Determine GitHub linkage from user or DB
    github_linked = bool(user.get("github_id"))
    if not github_linked:
        stored = await get_user_by_id(db, user["user_id"])  # type: ignore
        github_linked = bool(stored and stored.get("github_id"))
    rows = await list_google_tokens(db, user["user_id"])  # type: ignore
    summary = _summarize_google_tokens(rows)
    return {
        "github_linked": github_linked,
        "google_linked": bool(summary),
        "github_expiry": None,
        "github_scopes": None,
        "google_integrations": summary,
    }


@router.get("/auth/me", response_model=UserResponse, tags=["Authentication"])
async def get_current_user_info(request: Request):
    """Get current authenticated user information.
    
    This endpoint uses get_current_user dependency internally for compatibility.
    Architectural change: Uses Request parameter directly for Cloudflare Workers compatibility,
    but calls get_current_user internally to honor its 401 behavior.
    """
    # Call get_current_user internally to honor its 401 behavior
    # This maintains compatibility with existing clients while allowing Request parameter
    user = await get_current_user(request)
    
    # Fetch created_at from database if not in user dict
    created_at = None
    if not user.get("created_at"):
        try:
            db = ensure_db()
            db_user = await get_user_by_id(db, user["user_id"])
            if db_user:
                created_at_raw = db_user.get("created_at")
                if created_at_raw:
                    if isinstance(created_at_raw, datetime):
                        created_at = created_at_raw
                    else:
                        # Parse string timestamp
                        try:
                            created_at = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
                            if created_at.tzinfo is None:
                                created_at = created_at.replace(tzinfo=timezone.utc)
                        except (ValueError, AttributeError):
                            pass
        except Exception:
            # If DB fetch fails, use current time as fallback
            pass
    
    # Use current time as fallback if created_at not available
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    
    return UserResponse(
        user_id=user.get("user_id", "unknown"),
        github_id=user.get("github_id"),
        email=user.get("email"),
        created_at=created_at,
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
        payload=request.model_dump(),
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
