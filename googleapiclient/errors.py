"""Minimal HttpError stub."""
from __future__ import annotations

from typing import Any


class HttpError(Exception):
    def __init__(self, resp: Any = None, content: Any = None):
        # Build informative message from response and content
        status = None
        reason = None
        try:
            status = getattr(resp, "status", None)
        except Exception:
            status = None
        try:
            reason = getattr(resp, "reason", None)
        except Exception:
            reason = None
        if isinstance(content, (bytes, bytearray)):
            try:
                content_str = content.decode("utf-8", errors="replace")
            except Exception:
                content_str = repr(content)
        else:
            content_str = str(content) if content is not None else ""
        content_str = content_str.strip()
        parts = ["HTTP error"]
        if status is not None:
            parts.append(f"status={status}")
        if reason:
            parts.append(f"reason={reason}")
        if content_str:
            parts.append(f"content={content_str}")
        message = "; ".join(parts)
        super().__init__(message)
        self.resp = resp
        self.content = content
