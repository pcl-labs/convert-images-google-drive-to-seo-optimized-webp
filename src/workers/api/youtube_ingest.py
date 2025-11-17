from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import settings
from .database import get_document, update_document, record_usage_event
from .google_oauth import build_youtube_service_for_user
from ..core.youtube_captions import fetch_captions_text, YouTubeCaptionsError


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
    cap = await asyncio.to_thread(fetch_captions_text, yt_service, youtube_video_id, langs)
    if not cap.get("success"):
        raise YouTubeCaptionsError(cap.get("error") or "Captions unavailable for this video.")

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

    job_output = {
        "document_id": document_id,
        "youtube_video_id": youtube_video_id,
        "transcript": transcript_meta,
        "metadata": {
            "frontmatter": frontmatter,
            "youtube": video_meta,
        },
    }

    return {
        "text": text,
        "transcript_meta": transcript_meta,
        "frontmatter": frontmatter,
        "video_meta": video_meta,
        "document_metadata": metadata,
        "job_output": job_output,
    }
