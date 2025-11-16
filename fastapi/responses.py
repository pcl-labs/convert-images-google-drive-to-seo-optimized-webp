"""Minimal response helpers for the FastAPI stub."""
from __future__ import annotations

from typing import Any, Optional


class Response:
    def __init__(self, content: Any = None, status_code: int = 200, headers: Optional[dict[str, str]] = None, media_type: str | None = None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type


class JSONResponse(Response):
    def __init__(self, content: Any, status_code: int = 200, headers: Optional[dict[str, str]] = None):
        super().__init__(content=content, status_code=status_code, headers=headers, media_type="application/json")


class HTMLResponse(Response):
    def __init__(self, content: str, status_code: int = 200, headers: Optional[dict[str, str]] = None):
        super().__init__(content=content, status_code=status_code, headers=headers, media_type="text/html")


class PlainTextResponse(Response):
    def __init__(self, content: str, status_code: int = 200, headers: Optional[dict[str, str]] = None):
        super().__init__(content=content, status_code=status_code, headers=headers, media_type="text/plain")


class RedirectResponse(Response):
    def __init__(self, url: str, status_code: int = 302, headers: Optional[dict[str, str]] = None):
        hdrs = {"Location": url}
        if headers:
            hdrs.update(headers)
        super().__init__(content="", status_code=status_code, headers=hdrs)
