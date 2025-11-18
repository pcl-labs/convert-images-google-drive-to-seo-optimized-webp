"""YouTube Data API helpers implemented with urllib helpers."""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional

from .google_clients import GoogleAPIError, YouTubeClient


class YouTubeAPIError(Exception):
    pass


_CHAPTER_LINE_RE = re.compile(
    r"^\s*(?:[-–—•*]\s*)?(?P<timestamp>(?:\d{1,2}:)?\d{1,2}:\d{2})\s*(?:[-–—:]\s*|\s+)?(?P<title>.+)$"
)


def _timestamp_to_seconds(value: str) -> Optional[int]:
    if not value:
        return None
    parts = value.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
    else:
        hours, minutes, seconds = parts
    if seconds >= 60 or minutes >= 60 or hours < 0 or minutes < 0 or seconds < 0:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _parse_description_chapters(description: str) -> List[Dict[str, Any]]:
    """Extract timestamped chapter markers from a YouTube description."""
    chapters: List[Dict[str, Any]] = []
    if not description:
        return chapters
    seen_seconds: set[int] = set()
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _CHAPTER_LINE_RE.match(line)
        if not match:
            continue
        timestamp = match.group("timestamp")
        seconds = _timestamp_to_seconds(timestamp)
        if seconds is None or seconds in seen_seconds:
            continue
        title = (match.group("title") or "").strip()
        if not title:
            title = f"Chapter {len(chapters) + 1}"
        chapters.append({
            "title": title[:160],
            "timestamp": timestamp,
            "start_seconds": seconds,
        })
        seen_seconds.add(seconds)
    # Require at least two markers to avoid false positives from random timestamps
    if len(chapters) < 2:
        return []
    chapters.sort(key=lambda item: item.get("start_seconds") or 0)
    return chapters


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
    description = snippet.get("description") or ""
    chapters = _parse_description_chapters(description)
    frontmatter = {
        "title": snippet.get("title") or "Untitled",
        "description": description,
        "tags": tags,
        "channel_title": snippet.get("channelTitle") or "",
    }
    metadata = {
        "video_id": video_id,
        "title": snippet.get("title") or "Untitled",
        "description": description,
        "channel_title": snippet.get("channelTitle") or "",
        "channel_id": snippet.get("channelId") or "",
        "published_at": snippet.get("publishedAt") or "",
        "thumbnails": snippet.get("thumbnails") or {},
        "category_id": snippet.get("categoryId") or "",
        "tags": tags or [],
        "duration_seconds": duration_seconds,
        "live_broadcast_content": snippet.get("liveBroadcastContent") or "",
    }
    if chapters:
        metadata["chapters"] = chapters
    return {"frontmatter": frontmatter, "metadata": metadata}


__all__ = ["fetch_video_metadata", "YouTubeAPIError"]
