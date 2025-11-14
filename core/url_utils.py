import re
from typing import Optional

_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{6,})")


def parse_youtube_video_id(url: str) -> Optional[str]:
    if not isinstance(url, str):
        return None
    s = url.strip()
    if not s:
        return None
    m = _YT_ID_RE.search(s)
    return m.group(1) if m else None
