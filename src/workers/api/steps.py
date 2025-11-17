from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from googleapiclient.errors import HttpError

 

from .deps import ensure_db, get_current_user
from .database import (
    get_document,
    update_document,
    record_usage_event,
    get_step_invocation,
    save_step_invocation,
    record_pipeline_event,
)
from .google_oauth import build_youtube_service_for_user
from src.workers.core.youtube_api import fetch_video_metadata, YouTubeAPIError
from src.workers.core.youtube_captions import fetch_captions_text
from src.workers.core.ai_modules import generate_outline, organize_chapters, compose_blog, default_title_from_outline
from .app_logging import get_logger
from .drive_docs import (
    dict_from_field as _dict_from_field,
    merge_metadata_for_updates as _merge_metadata_for_updates,
    retry_update_document as _retry_update_document,
    schedule_drive_reconcile_job as _schedule_drive_reconcile_job,
    sync_drive_doc_after_persist as _sync_drive_doc_after_persist,
)
logger = get_logger(__name__)

DRIVE_SYNC_EXCEPTIONS = (HttpError, RuntimeError, OSError)

router = APIRouter(prefix="/api/v1/steps", tags=["Steps"])


class StepBase(BaseModel):
    job_id: Optional[str] = Field(default=None, description="Optional job identifier for usage tracking.")


class TranscriptFetchRequest(StepBase):
    video_id: str = Field(..., min_length=5, max_length=64)
    langs: List[str] = Field(default_factory=lambda: ["en"])
    document_id: Optional[str] = Field(default=None, description="Document to update with the transcript text.")

    @model_validator(mode="after")
    def validate_langs(self) -> "TranscriptFetchRequest":
        if not self.langs:
            self.langs = ["en"]
        return self


class OutlineGenerateRequest(StepBase):
    document_id: Optional[str] = None
    text: Optional[str] = None
    options: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ensure_text(self) -> "OutlineGenerateRequest":
        if not (self.document_id or self.text):
            raise ValueError("Either document_id or text must be provided")
        return self


class ChaptersOrganizeRequest(StepBase):
    document_id: Optional[str] = None
    text: Optional[str] = None

    @model_validator(mode="after")
    def ensure_text(self) -> "ChaptersOrganizeRequest":
        if not (self.document_id or self.text):
            raise ValueError("Either document_id or text must be provided")
        return self


class BlogComposeRequest(StepBase):
    document_id: Optional[str] = None
    outline: Optional[List[Dict[str, Any]]] = None
    chapters: Optional[List[Dict[str, Any]]] = None
    tone: str = "informative"

    @model_validator(mode="after")
    def ensure_structure(self) -> "BlogComposeRequest":
        if not (self.outline or self.chapters or self.document_id):
            raise ValueError("Provide outline, chapters, or document_id")
        return self


class DocumentPersistRequest(StepBase):
    document_id: str
    raw_text: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


def _require_idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key header is required")
    return key.strip()


def _payload_hash(payload: Dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def _check_idempotency(db, user_id: str, key: str, payload_hash: str) -> Optional[JSONResponse]:
    existing = await get_step_invocation(db, user_id, key)
    if not existing:
        return None
    if existing.get("request_hash") != payload_hash:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Idempotency conflict for provided key")
    body = json.loads(existing.get("response_body") or "{}")
    return JSONResponse(status_code=int(existing.get("status_code", 200)), content=body)


async def _finalize_idempotency(db, user_id: str, key: str, step_type: str, payload_hash: str, response_body: Dict[str, Any], status_code: int) -> None:
    await save_step_invocation(db, user_id, key, step_type, payload_hash, response_body, status_code)


async def _load_document_text(db, user_id: str, document_id: str) -> Dict[str, Any]:
    doc = await get_document(db, document_id, user_id=user_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


async def _record_step_pipeline_event(
    db,
    user_id: str,
    job_id: Optional[str],
    *,
    event_type: str,
    stage: str,
    status: str,
    message: str,
    document_id: Optional[str],
) -> None:
    if not job_id:
        return
    context: Dict[str, Any] = {}
    if document_id:
        context["document_id"] = document_id
    await record_pipeline_event(
        db,
        user_id,
        job_id,
        event_type=event_type,
        stage=stage,
        status=status,
        message=message,
        data={"document_id": document_id} if document_id else {},
        notify_level="success" if status == "completed" else None,
        notify_text=message,
        notify_context=context or None,
    )


def _outline_to_text(outline: List[Dict[str, Any]]) -> str:
    if not outline:
        return ""
    blocks: List[str] = []
    for idx, section in enumerate(outline, start=1):
        title = (section or {}).get("title") or f"Section {idx}"
        summary = (section or {}).get("summary") or ""
        summary = summary.strip()
        if summary:
            blocks.append(f"{idx}. {title}\n{summary}")
        else:
            blocks.append(f"{idx}. {title}")
    return "\n\n".join(blocks)


@router.post("/transcript.fetch")
async def transcript_fetch(request: Request, payload: TranscriptFetchRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    key = _require_idempotency_key(request)
    body = payload.model_dump()
    hash_val = _payload_hash(body)
    maybe_cached = await _check_idempotency(db, user["user_id"], key, hash_val)
    if maybe_cached:
        return maybe_cached

    langs = payload.langs or ["en"]
    try:
        yt_service = await build_youtube_service_for_user(db, user["user_id"])  # type: ignore
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Authoritative metadata (duration)
    try:
        meta_bundle = await asyncio.to_thread(fetch_video_metadata, yt_service, payload.video_id)
    except YouTubeAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # Captions text via official API
    cap = await asyncio.to_thread(fetch_captions_text, yt_service, payload.video_id, langs)
    if not cap.get("success"):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=cap.get("error") or "Captions unavailable")

    text = cap.get("text") or ""
    # Extract duration from authoritative API metadata bundle (available for both branches)
    duration_out = None
    try:
        youtube_meta = meta_bundle.get("metadata") or {}
        duration_out = youtube_meta.get("duration_seconds")
    except Exception:
        duration_out = None
    if payload.document_id:
        doc = await _load_document_text(db, user["user_id"], payload.document_id)
        existing_meta_raw = doc.get("metadata")
        try:
            existing_meta = json.loads(existing_meta_raw) if isinstance(existing_meta_raw, str) else (existing_meta_raw or {})
        except Exception:
            existing_meta = {}
        # Prefer authoritative API duration from the metadata bundle.
        new_meta = {
            **(existing_meta or {}),
            "source": "youtube",
            "lang": cap.get("lang"),
            # Precedence: API duration in metadata
            "duration_s": duration_out,
        }
        await update_document(
            db,
            payload.document_id,
            {
                "raw_text": text,
                "metadata": new_meta,
            },
        )

    response_body = {
        "document_id": payload.document_id,
        "text": text,
        "lang": cap.get("lang"),
        # Use API-provided duration
        "duration_s": duration_out,
        "source": cap.get("source"),
    }
    if payload.job_id:
        await record_usage_event(
            db,
            user["user_id"],
            payload.job_id,
            "transcribe",
            {"duration_s": duration_out, "lang": cap.get("lang")},
        )
    await _finalize_idempotency(db, user["user_id"], key, "transcript.fetch", hash_val, response_body, status.HTTP_200_OK)
    return response_body


@router.post("/outline.generate")
async def outline_generate(request: Request, payload: OutlineGenerateRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    key = _require_idempotency_key(request)
    body = payload.model_dump()
    hash_val = _payload_hash(body)
    maybe_cached = await _check_idempotency(db, user["user_id"], key, hash_val)
    if maybe_cached:
        return maybe_cached

    source_text = payload.text
    doc: Optional[Dict[str, Any]] = None
    if payload.document_id:
        doc = await _load_document_text(db, user["user_id"], payload.document_id)
        source_text = source_text or (doc.get("raw_text") or "")
    # Validate non-empty input
    if not source_text or not str(source_text).strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source text is required to generate outline")
    max_sections = 5
    try:
        if isinstance(payload.options, dict):
            max_sections = int(payload.options.get("max_sections", 5))
    except Exception:
        max_sections = 5
    # Run in a worker thread to avoid blocking event loop
    outline = await asyncio.to_thread(generate_outline, source_text, max_sections)
    outline_text_blocks = []
    for item in outline:
        title = (item or {}).get("title")
        summary = (item or {}).get("summary")
        if not title and not summary:
            continue
        if title:
            outline_text_blocks.append(f"{title}")
        if summary:
            outline_text_blocks.append(summary.strip())
    outline_text = "\n\n".join(outline_text_blocks).strip()
    if payload.document_id and doc:
        merged_meta = _dict_from_field(doc.get("metadata"))
        merged_meta["latest_outline"] = outline
        merged_meta["drive_stage"] = "outline"
        updates = {
            "metadata": merged_meta,
            "drive_text": outline_text,
        }
        await update_document(db, payload.document_id, updates)
        updated_doc = await _load_document_text(db, user["user_id"], payload.document_id)
        try:
            await _sync_drive_doc_after_persist(db, user["user_id"], updated_doc, updates)
        except DRIVE_SYNC_EXCEPTIONS as exc:
            logger.exception(
                "drive_sync_outline_failed",
                extra={"document_id": payload.document_id, "user_id": user["user_id"]},
                exc_info=True,
            )
            await _schedule_drive_reconcile_job(
                db,
                payload.document_id,
                user["user_id"],
                updated_doc.get("drive_file_id"),
                metadata_snapshot=_dict_from_field(updated_doc.get("metadata")),
            )
        except Exception:
            raise
    response_body = {"outline": outline, "document_id": payload.document_id, "text": outline_text}
    if payload.job_id:
        await record_usage_event(
            db,
            user["user_id"],
            payload.job_id,
            "outline",
            {"sections": len(outline), "chars": len(source_text or "")},
        )
        await _record_step_pipeline_event(
            db,
            user["user_id"],
            payload.job_id,
            event_type="outline.generate",
            stage="outline.generate",
            status="completed",
            message="Outline generated",
            document_id=payload.document_id,
        )
    await _finalize_idempotency(db, user["user_id"], key, "outline.generate", hash_val, response_body, status.HTTP_200_OK)
    return response_body


@router.post("/chapters.organize")
async def chapters_organize(request: Request, payload: ChaptersOrganizeRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    key = _require_idempotency_key(request)
    body = payload.model_dump()
    hash_val = _payload_hash(body)
    maybe_cached = await _check_idempotency(db, user["user_id"], key, hash_val)
    if maybe_cached:
        return maybe_cached

    source_text = payload.text
    if payload.document_id:
        doc = await _load_document_text(db, user["user_id"], payload.document_id)
        source_text = source_text or (doc.get("raw_text") or "")
    if not source_text or not str(source_text).strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source text is required to organize chapters")
    chapters = await asyncio.to_thread(organize_chapters, source_text)
    response_body = {"chapters": chapters, "document_id": payload.document_id}
    if payload.job_id:
        await record_usage_event(
            db,
            user["user_id"],
            payload.job_id,
            "chapters",
            {"chapters": len(chapters), "chars": len(source_text or "")},
        )
        await _record_step_pipeline_event(
            db,
            user["user_id"],
            payload.job_id,
            event_type="chapters.organize",
            stage="chapters.organize",
            status="completed",
            message="Chapters organized",
            document_id=payload.document_id,
        )
    await _finalize_idempotency(db, user["user_id"], key, "chapters.organize", hash_val, response_body, status.HTTP_200_OK)
    return response_body


@router.post("/blog.compose")
async def blog_compose(request: Request, payload: BlogComposeRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    key = _require_idempotency_key(request)
    body = payload.model_dump()
    hash_val = _payload_hash(body)
    maybe_cached = await _check_idempotency(db, user["user_id"], key, hash_val)
    if maybe_cached:
        return maybe_cached

    outline = payload.outline or []
    chapters = payload.chapters
    doc = None
    if payload.document_id and not (outline or chapters):
        doc = await _load_document_text(db, user["user_id"], payload.document_id)
        text = doc.get("raw_text") or ""
        if not text or not text.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Document has no text to compose from")
        # Run outline and chapters generation concurrently in threads
        outline_task = asyncio.to_thread(generate_outline, text, 5)
        chapters_task = asyncio.to_thread(organize_chapters, text)
        outline, chapters = await asyncio.gather(outline_task, chapters_task)
    chapters = chapters or [{"title": item.get("title"), "summary": item.get("summary")} for item in (outline or [])]
    composed = await compose_blog(chapters, tone=payload.tone)
    if payload.document_id:
        # Merge metadata to avoid dropping existing fields
        if doc is None:
            doc = await _load_document_text(db, user["user_id"], payload.document_id)
        existing_meta_raw = doc.get("metadata")
        try:
            existing_meta = json.loads(existing_meta_raw) if isinstance(existing_meta_raw, str) else (existing_meta_raw or {})
        except Exception:
            existing_meta = {}
        merged_meta = {**(existing_meta or {})}
        merged_meta.update({
            "last_composed": datetime.now(timezone.utc).isoformat(),
            "tone": payload.tone,
            "title": default_title_from_outline(outline or chapters or []),
        })
        await update_document(
            db,
            payload.document_id,
            {
                "metadata": merged_meta,
            },
        )
    response_body = {"document_id": payload.document_id, **composed}
    if payload.job_id:
        await record_usage_event(
            db,
            user["user_id"],
            payload.job_id,
            "compose",
            {"sections": len(chapters or []), "word_count": composed["meta"].get("word_count", 0)},
        )
        await _record_step_pipeline_event(
            db,
            user["user_id"],
            payload.job_id,
            event_type="blog.compose",
            stage="blog.compose",
            status="completed",
            message="Blog composed",
            document_id=payload.document_id,
        )
    await _finalize_idempotency(db, user["user_id"], key, "blog.compose", hash_val, response_body, status.HTTP_200_OK)
    return response_body


@router.post("/document.persist")
async def document_persist(request: Request, payload: DocumentPersistRequest, user: dict = Depends(get_current_user)):
    db = ensure_db()
    key = _require_idempotency_key(request)
    body = payload.model_dump()
    hash_val = _payload_hash(body)
    maybe_cached = await _check_idempotency(db, user["user_id"], key, hash_val)
    if maybe_cached:
        return maybe_cached

    document = await _load_document_text(db, user["user_id"], payload.document_id)
    updates: Dict[str, Any] = {}
    if payload.raw_text is not None:
        updates["raw_text"] = payload.raw_text
    if payload.metadata is not None:
        updates["metadata"] = payload.metadata
    if updates:
        requested_fields = list(updates.keys())
        merged_metadata = _merge_metadata_for_updates(
            _dict_from_field(document.get("metadata")),
            updates.get("metadata") if isinstance(updates.get("metadata"), dict) else {},
            status="pending",
        )
        updates["metadata"] = merged_metadata
        await update_document(db, payload.document_id, updates)
        updated_doc = await _load_document_text(db, user["user_id"], payload.document_id)
        try:
            await _sync_drive_doc_after_persist(db, user["user_id"], updated_doc, updates)
        except DRIVE_SYNC_EXCEPTIONS as exc:
            logger.exception(
                "drive_sync_after_persist_failed",
                extra={
                    "document_id": payload.document_id,
                    "user_id": user["user_id"],
                    "updates": list(updates.keys()),
                },
                exc_info=True,
            )
            await _schedule_drive_reconcile_job(
                db,
                payload.document_id,
                user["user_id"],
                updated_doc.get("drive_file_id"),
                metadata_snapshot=_dict_from_field(updated_doc.get("metadata")),
            )
        except Exception:
            raise
        response_body = {"document_id": payload.document_id, "updated": requested_fields}
    else:
        response_body = {"document_id": payload.document_id, "updated": []}
    if payload.job_id:
        await record_usage_event(
            db,
            user["user_id"],
            payload.job_id,
            "persist",
            {"fields": response_body["updated"]},
        )
    await _finalize_idempotency(db, user["user_id"], key, "document.persist", hash_val, response_body, status.HTTP_200_OK)
    return response_body
