"""Transcript helper built on youtube-transcript-api."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, Optional

from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    IpBlocked,
    NoTranscriptFound,
    NotTranslatable,
    RequestBlocked,
    TranscriptsDisabled,
    TranslationLanguageNotAvailable,
    VideoUnavailable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)
from youtube_transcript_api.formatters import TextFormatter

logger = logging.getLogger(__name__)


class TranscriptProxyError(Exception):
    """Unified error wrapper for transcript fetch failures."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Optional[Any] = None,
        status_code: int = 200,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.status_code = status_code


async def fetch_transcript_via_proxy(video_id: str) -> Dict[str, Any]:
    """Fetch transcript using youtube-transcript-api (working-example parity)."""
    if not video_id or not isinstance(video_id, str) or len(video_id) != 11:
        raise ValueError("Invalid video_id: must be 11 characters")

    try:
        return await asyncio.to_thread(_fetch_transcript_sync, video_id)
    except TranscriptProxyError:
        raise
    except Exception as exc:
        logger.error("youtube_transcript_unexpected_error", exc_info=True, extra={"video_id": video_id})
        raise TranscriptProxyError("unknown", f"Unexpected error: {exc}") from exc


def _fetch_transcript_sync(video_id: str) -> Dict[str, Any]:
    formatter = TextFormatter()
    api = YouTubeTranscriptApi()
    try:
        transcript_list = api.list(video_id)
        transcript = _select_transcript(transcript_list)
        transcript_data = transcript.fetch()
        transcript_text = (formatter.format_transcript(transcript_data) or "").strip()
        if not transcript_text:
            raise TranscriptProxyError("no_captions", "Transcript text is empty")

        language = getattr(transcript, "language_code", None)
        is_generated = getattr(transcript, "is_generated", False)

        return {
            "success": True,
            "transcript": {
                "text": transcript_text,
                "format": "text",
                "language": language,
                "trackKind": "asr" if is_generated else "manual",
            },
            "metadata": {
                "clientVersion": None,
                "method": "youtube-transcript-api",
                "videoId": video_id,
            },
        }

    except (TranscriptsDisabled, NoTranscriptFound) as exc:
        raise TranscriptProxyError("no_captions", str(exc)) from exc
    except VideoUnavailable as exc:
        raise TranscriptProxyError("invalid_video", "Video is unavailable or private") from exc
    except (RequestBlocked, IpBlocked) as exc:
        raise TranscriptProxyError("blocked", "YouTube blocked the request") from exc
    except (TranslationLanguageNotAvailable, NotTranslatable) as exc:
        raise TranscriptProxyError("no_captions", str(exc)) from exc
    except CouldNotRetrieveTranscript as exc:
        raise TranscriptProxyError("network_error", str(exc)) from exc
    except YouTubeRequestFailed as exc:
        raise TranscriptProxyError("network_error", str(exc)) from exc


def _select_transcript(transcript_list: Any) -> Any:
    """Replicate working-example fallback strategy."""
    preferences: Iterable[Iterable[str]] = [
        ("en",),
        ("en-US", "en-GB", "en"),
    ]
    for langs in preferences:
        try:
            return transcript_list.find_transcript(list(langs))
        except (NoTranscriptFound, TranscriptsDisabled):
            continue

    # Prefer manually created English transcripts if available
    try:
        return transcript_list.find_manually_created_transcript(["en", "en-US", "en-GB"])
    except Exception:
        pass

    # Fallback to generated tracks
    try:
        return transcript_list.find_generated_transcript(["en", "en-US", "en-GB"])
    except Exception:
        pass

    # Try translating a few common languages to English
    translation_sources = [
        ("es", "es-419", "es-US"),
        ("fr",),
        ("de",),
    ]
    for langs in translation_sources:
        try:
            original = transcript_list.find_transcript(list(langs))
            return original.translate("en")
        except (NoTranscriptFound, TranscriptsDisabled, NotTranslatable, TranslationLanguageNotAvailable):
            continue

    raise TranscriptProxyError("no_captions", "No suitable transcript found")
