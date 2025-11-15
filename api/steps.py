from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from .deps import ensure_db, get_current_user
from .database import (
    get_document,
    update_document,
    record_usage_event,
    get_step_invocation,
    save_step_invocation,
)
from core.transcripts import fetch_transcript_with_fallback
from core.ai_modules import generate_outline, organize_chapters, compose_blog, default_title_from_outline


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
    result = fetch_transcript_with_fallback(payload.video_id, langs)
    if not result.get("success"):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result.get("error") or "Transcript unavailable")

    text = result.get("text") or ""
    if payload.document_id:
        await _load_document_text(db, user["user_id"], payload.document_id)
        await update_document(
            db,
            payload.document_id,
            {
                "raw_text": text,
                "metadata": {
                    "source": "youtube",
                    "lang": result.get("lang"),
                    "duration_s": result.get("duration_s"),
                },
            },
        )

    response_body = {
        "document_id": payload.document_id,
        "text": text,
        "lang": result.get("lang"),
        "duration_s": result.get("duration_s"),
        "source": result.get("source"),
    }
    if payload.job_id:
        await record_usage_event(
            db,
            user["user_id"],
            payload.job_id,
            "transcribe",
            {"duration_s": result.get("duration_s"), "lang": result.get("lang")},
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
    if payload.document_id:
        doc = await _load_document_text(db, user["user_id"], payload.document_id)
        source_text = source_text or (doc.get("raw_text") or "")
    outline = generate_outline(source_text or "", payload.options.get("max_sections", 5))
    response_body = {"outline": outline, "document_id": payload.document_id}
    if payload.job_id:
        await record_usage_event(
            db,
            user["user_id"],
            payload.job_id,
            "outline",
            {"sections": len(outline), "chars": len(source_text or "")},
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
    chapters = organize_chapters(source_text or "")
    response_body = {"chapters": chapters, "document_id": payload.document_id}
    if payload.job_id:
        await record_usage_event(
            db,
            user["user_id"],
            payload.job_id,
            "chapters",
            {"chapters": len(chapters), "chars": len(source_text or "")},
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
    if payload.document_id and not (outline or chapters):
        doc = await _load_document_text(db, user["user_id"], payload.document_id)
        text = doc.get("raw_text") or ""
        outline = generate_outline(text, 5)
        chapters = organize_chapters(text)
    chapters = chapters or [{"title": item.get("title"), "summary": item.get("summary")} for item in (outline or [])]
    composed = compose_blog(chapters, tone=payload.tone)
    if payload.document_id:
        await update_document(
            db,
            payload.document_id,
            {
                "metadata": {
                    "last_composed": uuid.uuid4().hex,
                    "tone": payload.tone,
                    "title": default_title_from_outline(outline or chapters or []),
                }
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

    await _load_document_text(db, user["user_id"], payload.document_id)
    updates: Dict[str, Any] = {}
    if payload.raw_text is not None:
        updates["raw_text"] = payload.raw_text
    if payload.metadata is not None:
        updates["metadata"] = payload.metadata
    if updates:
        await update_document(db, payload.document_id, updates)
    response_body = {"document_id": payload.document_id, "updated": list(updates.keys())}
    if payload.job_id:
        await record_usage_event(
            db,
            user["user_id"],
            payload.job_id,
            "persist",
            {"fields": list(updates.keys())},
        )
    await _finalize_idempotency(db, user["user_id"], key, "document.persist", hash_val, response_body, status.HTTP_200_OK)
    return response_body
