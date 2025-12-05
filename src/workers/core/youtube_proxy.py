"""Transcript helper built on youtube-transcript-api."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Iterable, Optional

import httpx

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

YOUTUBE_CAPTIONS_API = "https://youtube.googleapis.com/youtube/v3/captions"


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
        # Use run_in_executor instead of asyncio.to_thread for better Workers compatibility
        # This pattern is used elsewhere in the codebase (see google_async.py)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _fetch_transcript_sync, video_id)
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
    except (NoTranscriptFound, TranscriptsDisabled):
        pass

    # Fallback to generated tracks
    try:
        return transcript_list.find_generated_transcript(["en", "en-US", "en-GB"])
    except (NoTranscriptFound, TranscriptsDisabled):
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


async def fetch_transcript_via_youtube_api(video_id: str, access_token: str) -> Dict[str, Any]:
    """Fetch transcript using YouTube Data API with OAuth access token."""
    if not video_id or not isinstance(video_id, str) or len(video_id) != 11:
        raise ValueError("Invalid video_id: must be 11 characters")
    if not access_token:
        raise TranscriptProxyError("auth_failed", "YouTube access token is missing")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    params = {
        "part": "id,snippet",
        "videoId": video_id,
        "maxResults": 50,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(YOUTUBE_CAPTIONS_API, headers=headers, params=params)
        except httpx.HTTPError as exc:
            logger.error("youtube_api_network_error", exc_info=True, extra={"video_id": video_id})
            raise TranscriptProxyError("network_error", "Failed to reach YouTube API") from exc

        if response.status_code == 403:
            raise TranscriptProxyError("permission_denied", "YouTube API access forbidden")
        if response.status_code == 404:
            raise TranscriptProxyError("invalid_video", "Video not found on YouTube")
        if response.status_code == 401:
            raise TranscriptProxyError("auth_failed", "YouTube access token is invalid or expired")

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "youtube_captions_http_error",
                extra={"video_id": video_id, "status": exc.response.status_code},
            )
            raise TranscriptProxyError("network_error", "YouTube captions API request failed") from exc

        data = response.json()
        items = data.get("items") or []
        best_caption = _select_caption_item(items)
        if not best_caption:
            raise TranscriptProxyError("no_captions", "No suitable captions available via YouTube API")
        caption_id = best_caption["id"]
        snippet = best_caption.get("snippet") or {}

        download_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/plain",
        }
        download_params = {"tfmt": "vtt", "alt": "media"}
        try:
            download_response = await client.get(
                f"{YOUTUBE_CAPTIONS_API}/{caption_id}",
                headers=download_headers,
                params=download_params,
            )
        except httpx.HTTPError as exc:
            logger.error("youtube_caption_download_error", exc_info=True, extra={"video_id": video_id})
            raise TranscriptProxyError("network_error", "Failed to download YouTube captions") from exc

        if download_response.status_code == 403:
            raise TranscriptProxyError("permission_denied", "YouTube denied access to caption file")
        if download_response.status_code == 404:
            raise TranscriptProxyError("no_captions", "Caption track no longer exists")
        try:
            download_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TranscriptProxyError("network_error", "YouTube caption download failed") from exc

        transcript_text = _parse_vtt_text(download_response.text)
        if not transcript_text:
            raise TranscriptProxyError("no_captions", "Transcript text is empty")

        return {
            "success": True,
            "transcript": {
                "text": transcript_text,
                "format": "text",
                "language": snippet.get("language"),
                "trackKind": snippet.get("trackKind"),
            },
            "metadata": {
                "clientVersion": None,
                "method": "youtube-api",
                "videoId": video_id,
                "captionId": caption_id,
            },
        }


def _select_caption_item(items: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best = None
    best_score = (10, 10, 10)
    for item in items:
        snippet = item.get("snippet") or {}
        language = (snippet.get("language") or "").lower()
        track_kind = (snippet.get("trackKind") or "").lower()
        if not item.get("id"):
            continue
        is_asr = track_kind == "asr"
        prefer_lang = 0 if language.startswith("en") else 1
        prefer_manual = 0 if not is_asr else 1
        prefer_named = 0 if snippet.get("name") else 1
        score = (prefer_manual, prefer_lang, prefer_named)
        if score < best_score:
            best = item
            best_score = score
    return best


def _parse_vtt_text(vtt_text: str) -> str:
    lines: list[str] = []
    for line in vtt_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("WEBVTT"):
            continue
        if "-->" in stripped:
            continue
        if stripped.isdigit():
            continue
        lines.append(stripped)
    return _normalize_whitespace(" ".join(lines))


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
