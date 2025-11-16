"""YouTube captions helpers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .google_clients import GoogleAPIError, YouTubeClient


class YouTubeCaptionsError(Exception):
    pass


def _select_caption_track(items: List[Dict[str, Any]], langs: List[str]) -> Optional[Dict[str, Any]]:
    prefs = [lang.strip().lower() for lang in (langs or ["en"]) if isinstance(lang, str) and lang.strip()]
    def score(item: Dict[str, Any]) -> int:
        snippet = item.get("snippet", {})
        lang = (snippet.get("language") or snippet.get("languageCode") or "").lower()
        if lang in prefs:
            return 2
        for p in prefs:
            if lang.startswith(p + "-"):
                return 1
        return 0
    best = None
    best_score = 0
    for it in items:
        s = score(it)
        if s > best_score:
            best = it
            best_score = s
    return best


def fetch_captions_text(client: YouTubeClient, video_id: str, langs: List[str]) -> Dict[str, Any]:
    try:
        resp = client.list_captions(video_id)
    except GoogleAPIError as exc:
        raise YouTubeCaptionsError(f"YouTube Captions API error: {exc}") from exc
    items = resp.get("items") or []
    if not items:
        return {"success": False, "error": "No captions available for this video (owner-only captions)."}
    chosen = _select_caption_track(items, langs or ["en"])
    if not chosen:
        return {"success": False, "error": "No captions match requested languages."}
    cap_id = chosen.get("id")
    snippet = chosen.get("snippet", {})
    lang = snippet.get("language") or snippet.get("languageCode") or ""
    try:
        text = client.download_caption(cap_id, format="srt")
    except GoogleAPIError as exc:
        raise YouTubeCaptionsError(f"Failed to download captions: {exc}") from exc
    text = (text or "").strip()
    if not text:
        return {"success": False, "error": "Downloaded captions are empty."}
    return {"success": True, "text": text, "lang": lang or None, "source": "captions"}


__all__ = ["fetch_captions_text", "YouTubeCaptionsError"]
