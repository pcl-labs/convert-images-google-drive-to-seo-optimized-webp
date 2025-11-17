from __future__ import annotations

from typing import Any, Dict, List, Optional

from .google_clients import GoogleAPIError, GoogleHTTPError, YouTubeClient


def _select_caption_track(items: List[Dict[str, Any]], langs: List[str]) -> Optional[Dict[str, Any]]:
    # Normalize langs like ["en", "en-US"] preference
    prefs = [lang.strip().lower() for lang in (langs or ["en"]) if isinstance(lang, str) and lang.strip()]
    # Prefer exact language, then try primary language match (e.g., 'en' matches 'en-US')
    def score(item: Dict[str, Any]) -> int:
        snippet = item.get("snippet", {})
        lang = (snippet.get("language") or "").lower()
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


def _strip_srt_formatting(srt_text: str) -> str:
    """
    Extract plain text from SRT format by removing sequence numbers and timestamps.
    
    SRT format structure:
        1
        00:00:00,000 --> 00:00:02,000
        Caption text here
        Can be multiple lines
        
        2
        00:00:02,000 --> 00:00:04,000
        More caption text
    
    Returns plain text with caption content only, with blank lines between caption blocks.
    """
    lines = srt_text.splitlines()
    text_lines = []
    i = 0
    
    while i < len(lines):
        line = lines[i].strip()
        
        # Skip empty lines
        if not line:
            i += 1
            continue
        
        # Check if this line is a sequence number (numeric only)
        try:
            int(line)
            # This is a sequence number, skip it
            i += 1
            # Skip the timestamp line (next non-empty line should contain -->)
            if i < len(lines):
                timestamp_line = lines[i].strip()
                if "-->" in timestamp_line:
                    i += 1
            # Now collect all text lines until we hit the next sequence number or empty line
            caption_block = []
            while i < len(lines):
                current_line = lines[i].strip()
                # Stop at empty line (end of caption block)
                if not current_line:
                    i += 1
                    break
                # Stop if we hit another sequence number
                try:
                    int(current_line)
                    break
                except ValueError:
                    # Not a sequence number, check if it's a timestamp
                    if "-->" in current_line:
                        i += 1
                        continue
                    # This is caption text
                    caption_block.append(current_line)
                    i += 1
            
            # Add caption block with space between lines, then blank line between blocks
            if caption_block:
                text_lines.append(" ".join(caption_block))
            continue
        except ValueError:
            # Not a sequence number, check if it's a timestamp line
            if "-->" in line:
                i += 1
                continue
            
            # Fallback: treat as caption text (for edge cases)
            text_lines.append(line)
            i += 1
    
    # Join with newlines to preserve separation between caption blocks
    return "\n".join(text_lines)


def fetch_captions_text(service: YouTubeClient, video_id: str, langs: List[str]) -> Dict[str, Any]:
    """
    Fetch caption text via official YouTube Data API for a given video owned by the authenticated user.

    Returns { success, text, lang, source } or { success: False, error }.
    """
    try:
        resp = service.list_captions(video_id)
    except GoogleAPIError as exc:
        return {"success": False, "error": f"YouTube Captions API error: {exc}"}

    items = resp.get("items") or []
    if not items:
        return {"success": False, "error": "No captions available for this video (owner-only captions)."}

    chosen = _select_caption_track(items, langs or ["en"])
    if not chosen:
        return {"success": False, "error": "No captions match requested languages."}

    cap_id = chosen.get("id")
    snippet = chosen.get("snippet", {})
    lang = snippet.get("language") or ""

    try:
        srt_text = service.download_caption(cap_id, format="srt")
    except GoogleHTTPError as exc:
        return {"success": False, "error": f"Failed to download captions: {exc}"}

    # Extract plain text from SRT format by stripping sequence numbers and timestamps
    text = _strip_srt_formatting(srt_text)
    
    # Normalization: strip and ensure non-empty
    text = (text or "").strip()
    if not text:
        return {"success": False, "error": "Downloaded captions are empty."}

    return {"success": True, "text": text, "lang": lang or None, "source": "captions"}
