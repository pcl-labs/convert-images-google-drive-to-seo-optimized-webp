from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import textwrap
import re

from .config import settings
from .database import get_document, update_document, record_usage_event, record_pipeline_event
from .google_oauth import build_youtube_service_for_user
from ..core.youtube_captions import fetch_captions_text, YouTubeCaptionsError


logger = logging.getLogger(__name__)


async def _safe_record_pipeline_event(
    db,
    user_id: str,
    job_id: str,
    *,
    event_type: str,
    stage: str,
    status: str,
    message: str,
    data: Optional[Dict[str, Any]] = None,
    notify_level: Optional[str] = None,
    notify_text: Optional[str] = None,
    notify_context: Optional[Dict[str, Any]] = None,
):
    try:
        await record_pipeline_event(
            db,
            user_id,
            job_id,
            event_type=event_type,
            stage=stage,
            status=status,
            message=message,
            data=data or {},
            notify_level=notify_level,
            notify_text=notify_text,
            notify_context=notify_context,
        )
    except Exception as exc:
        logger.error(
            "Failed to record pipeline event",
            exc_info=True,
            extra={
                "user_id": user_id,
                "job_id": job_id,
                "event_type": event_type,
                "stage": stage,
                "status": status,
                "message": message,
            },
        )


def _parse_document_metadata(doc: Dict[str, Any]) -> Dict[str, Any]:
    metadata = doc.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if metadata is None:
        metadata = {}
    return metadata


def _json_dict_field(value: Any, default: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return dict(default)
    if isinstance(value, dict):
        return value
    return dict(default)


_KEYWORD_RE = re.compile(r"[A-Za-z0-9']+")
_COMMON_KEYWORDS = {
    "the",
    "and",
    "that",
    "with",
    "from",
    "your",
    "this",
    "about",
    "what",
    "will",
    "into",
    "have",
    "when",
}


def _chapter_keywords(text: str, limit: int = 4) -> List[str]:
    words: List[str] = []
    for word in _KEYWORD_RE.findall(str(text or "").lower()):
        if len(word) < 4 or word in _COMMON_KEYWORDS or word in words:
            continue
        words.append(word)
        if len(words) >= limit:
            break
    return words


def build_outline_from_chapters(chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    outline: List[Dict[str, Any]] = []
    if not chapters:
        return outline
    total = len(chapters)
    for idx, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            continue
        title = (chapter.get("title") or f"Chapter {idx + 1}").strip() or f"Chapter {idx + 1}"
        summary_hint = chapter.get("summary") or chapter.get("description") or ""
        if not summary_hint:
            ts = chapter.get("timestamp")
            summary_hint = f"Insights beginning at {ts}" if ts else "Key discussion point"
        summary = textwrap.shorten(str(summary_hint).strip(), width=320, placeholder="…")
        slot = "body"
        if idx == 0:
            slot = "intro"
        elif idx == total - 1:
            slot = "cta"
        outline.append(
            {
                "title": title[:160],
                "summary": summary,
                "slot": slot,
                "keywords": _chapter_keywords(title),
                "source": "youtube_chapter",
                "timestamp": chapter.get("timestamp"),
                "start_seconds": chapter.get("start_seconds"),
                "chapter_index": idx,
            }
        )
    return outline


async def ingest_youtube_document(
    db,
    job_id: str,
    user_id: str,
    document_id: str,
    youtube_video_id: str,
    payload_metadata: Dict[str, Any],
    payload_frontmatter: Dict[str, Any],
    duration_s: Optional[int],
) -> Dict[str, Any]:
    """Fetch captions, merge metadata, persist transcript, and return job output context."""
    document = await get_document(db, document_id, user_id=user_id)
    if not document:
        raise ValueError("Document not found")
    await _safe_record_pipeline_event(
        db,
        user_id,
        job_id,
        event_type="ingest_youtube",
        stage="document.load",
        status="completed",
        message="Document loaded",
        data={"document_id": document_id},
    )

    metadata = _parse_document_metadata(document)
    frontmatter = _json_dict_field(document.get("frontmatter"), {})
    if payload_frontmatter:
        frontmatter.update(payload_frontmatter)

    langs_raw = settings.transcript_langs
    if isinstance(langs_raw, str):
        langs = [s.strip() for s in langs_raw.split(",") if s.strip()]
    else:
        langs = langs_raw or ["en"]

    yt_service = await build_youtube_service_for_user(db, user_id)  # type: ignore
    await _safe_record_pipeline_event(
        db,
        user_id,
        job_id,
        event_type="ingest_youtube",
        stage="captions.fetch",
        status="running",
        message="Fetching captions",
        data={"video_id": youtube_video_id, "langs": langs},
    )
    cap = await asyncio.to_thread(fetch_captions_text, yt_service, youtube_video_id, langs)
    if not cap.get("success"):
        raise YouTubeCaptionsError(cap.get("error") or "Captions unavailable for this video.")
    await _safe_record_pipeline_event(
        db,
        user_id,
        job_id,
        event_type="ingest_youtube",
        stage="captions.fetch",
        status="completed",
        message="Captions fetched",
        data={"video_id": youtube_video_id, "lang": cap.get("lang"), "chars": len(cap.get("text") or "")},
    )

    text = (cap.get("text") or "").strip()
    source = cap.get("source") or "captions"
    lang = cap.get("lang") or "en"
    if duration_s is None:
        raise ValueError("Transcript fetch succeeded but duration is missing.")

    try:
        await record_usage_event(
            db,
            user_id,
            job_id,
            "transcribe",
            {"engine": "captions_api", "duration_s": duration_s},
        )
    except Exception:
        pass

    now_iso = datetime.now(timezone.utc).isoformat()
    video_meta = {}
    if isinstance(metadata.get("youtube"), dict):
        video_meta.update(metadata["youtube"])
    if isinstance(payload_metadata, dict):
        video_meta.update(payload_metadata)
    video_meta["duration_seconds"] = duration_s
    video_meta.setdefault("video_id", youtube_video_id)
    video_meta["fetched_at"] = now_iso

    transcript_meta = {
        "source": source,
        "lang": lang,
        "chars": len(text),
        "duration_s": duration_s,
        "fetched_at": now_iso,
    }

    metadata["source"] = "youtube"
    metadata["video_id"] = youtube_video_id
    metadata["lang"] = lang
    metadata["chars"] = len(text)
    metadata["updated_at"] = now_iso
    metadata["transcript_source"] = source
    metadata["youtube"] = video_meta
    metadata["transcript"] = transcript_meta
    metadata["latest_ingest_job_id"] = job_id
    metadata.setdefault("url", payload_metadata.get("url"))
    metadata.setdefault("title", payload_metadata.get("title") or frontmatter.get("title"))

    if "title" not in frontmatter and payload_metadata.get("title"):
        frontmatter["title"] = payload_metadata.get("title")

    raw_chapters = payload_metadata.get("chapters") or video_meta.get("chapters")
    if isinstance(raw_chapters, list):
        chapter_dicts = [chap for chap in raw_chapters if isinstance(chap, dict)]
        if chapter_dicts:
            metadata["youtube"]["chapters"] = chapter_dicts
            chapter_outline = build_outline_from_chapters(chapter_dicts)
            if chapter_outline:
                metadata["latest_outline"] = chapter_outline
                metadata.setdefault("outline_source", "youtube_chapters")

    await _safe_record_pipeline_event(
        db,
        user_id,
        job_id,
        event_type="ingest_youtube",
        stage="document.persist",
        status="running",
        message="Persisting transcript to document",
        data={"document_id": document_id},
    )
    await update_document(
        db,
        document_id,
        {
            "raw_text": text,
            "metadata": metadata,
            "frontmatter": frontmatter,
            "content_format": "youtube",
        },
    )
    await _safe_record_pipeline_event(
        db,
        user_id,
        job_id,
        event_type="ingest_youtube",
        stage="document.persist",
        status="completed",
        message="Document updated",
        data={"document_id": document_id},
    )

    job_output = {
        "document_id": document_id,
        "youtube_video_id": youtube_video_id,
        "transcript": transcript_meta,
        "metadata": {
            "frontmatter": frontmatter,
            "youtube": video_meta,
        },
    }

    await _safe_record_pipeline_event(
        db,
        user_id,
        job_id,
        event_type="ingest_youtube",
        stage="job.output",
        status="completed",
        message="YouTube ingest completed",
        data={"document_id": document_id},
    )

    return {
        "text": text,
        "transcript_meta": transcript_meta,
        "frontmatter": frontmatter,
        "video_meta": video_meta,
        "document_metadata": metadata,
        "job_output": job_output,
    }
