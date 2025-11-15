from __future__ import annotations

from typing import Any, Dict, List, Optional

from googleapiclient.errors import HttpError


class YouTubeCaptionsError(Exception):
    pass


def _select_caption_track(items: List[Dict[str, Any]], langs: List[str]) -> Optional[Dict[str, Any]]:
    # Normalize langs like ["en", "en-US"] preference
    prefs = [lang.strip().lower() for lang in (langs or ["en"]) if isinstance(lang, str) and lang.strip()]
    # Prefer exact languageCode, then try primary language match (e.g., 'en' matches 'en-US')
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
    # Require a positive match score; start at 0 so only s > 0 selects
    best_score = 0
    for it in items:
        s = score(it)
        if s > best_score:
            best = it
            best_score = s
    return best


def fetch_captions_text(service, video_id: str, langs: List[str]) -> Dict[str, Any]:
    """
    Fetch caption text via official YouTube Data API for a given video owned by the authenticated user.

    Returns { success, text, lang, source } or { success: False, error }.
    """
    try:
        req = service.captions().list(part="id,snippet", videoId=video_id)
        resp = req.execute()
    except HttpError as exc:
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
        # Download captions as plain text (defaults to .srt if not specified). Using tfmt='ttml' or 'srt' is possible; we want text.
        # Some APIs support 'tfmt' or 'tlang'; googleapiclient handles 'tfmt' via .download with 'tfmt' query param.
        req_dl = service.captions().download(id=cap_id, tfmt="srt")  # type: ignore[arg-type]
        # Media download requires the MediaIoBaseDownload pattern; however, googleapiclient supports .execute() for captions.download
        # which returns the body text directly when using built-in discovery.
        data = req_dl.execute()
        # data can be bytes or str depending on client; ensure str
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    except HttpError as exc:
        raise YouTubeCaptionsError(f"Failed to download captions: {exc}") from exc

    # Very light normalization: strip and ensure non-empty
    text = (text or "").strip()
    if not text:
        return {"success": False, "error": "Downloaded captions are empty."}

    return {"success": True, "text": text, "lang": lang or None, "source": "captions"}
