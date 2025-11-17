"""YouTube Data API helpers implemented with urllib helpers."""

from __future__ import annotations

import datetime
from typing import Any, Dict

from .google_clients import GoogleAPIError, YouTubeClient


class YouTubeAPIError(Exception):
    pass


def _parse_duration_iso8601(value: str) -> int:
    if not value or not value.startswith("P"):
        raise ValueError("Invalid duration")
    total = datetime.timedelta()
    time_part = False
    num = ""
    hours = minutes = seconds = 0
    for ch in value[1:]:
        if ch == "T":
            time_part = True
            continue
        if ch.isdigit():
            num += ch
            continue
        if not num:
            continue
        amount = int(num)
        num = ""
        if ch == "H":
            hours = amount
        elif ch == "M":
            if time_part:
                minutes = amount
        elif ch == "S":
            seconds = amount
        elif ch == "D":
            total += datetime.timedelta(days=amount)
    total += datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds)
    return int(total.total_seconds())


def fetch_video_metadata(client: YouTubeClient, video_id: str) -> Dict[str, Any]:
    try:
        response = client.fetch_video(video_id)
    except GoogleAPIError as exc:
        raise YouTubeAPIError(f"YouTube API error: {exc}") from exc
    items = response.get("items") or []
    if not items:
        raise YouTubeAPIError("Video not found or inaccessible")
    item = items[0]
    status = item.get("status", {})
    if status.get("privacyStatus") not in {"public", "unlisted"}:
        raise YouTubeAPIError("Video is private or restricted")
    snippet = item.get("snippet", {})
    content_details = item.get("contentDetails", {})
    duration_iso = content_details.get("duration")
    if not duration_iso:
        raise YouTubeAPIError("Video duration unavailable")
    try:
        duration_seconds = _parse_duration_iso8601(duration_iso)
    except ValueError as exc:
        raise YouTubeAPIError("Invalid duration format") from exc
    tags = snippet.get("tags") or []
    frontmatter = {
        "title": snippet.get("title") or "Untitled",
        "description": snippet.get("description") or "",
        "tags": tags,
        "channel_title": snippet.get("channelTitle") or "",
    }
    metadata = {
        "video_id": video_id,
        "title": snippet.get("title") or "Untitled",
        "description": snippet.get("description") or "",
        "channel_title": snippet.get("channelTitle") or "",
        "channel_id": snippet.get("channelId") or "",
        "published_at": snippet.get("publishedAt") or "",
        "thumbnails": snippet.get("thumbnails") or {},
        "category_id": snippet.get("categoryId") or "",
        "tags": tags or [],
        "duration_seconds": duration_seconds,
        "live_broadcast_content": snippet.get("liveBroadcastContent") or "",
    }
    return {"frontmatter": frontmatter, "metadata": metadata}


__all__ = ["fetch_video_metadata", "YouTubeAPIError"]
