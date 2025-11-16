"""Minimal StaticFiles placeholder for FastAPI stub."""
from __future__ import annotations

from typing import Any


class StaticFiles:
    def __init__(self, directory: str, html: bool = False, check_dir: bool = True):
        self.directory = directory
        self.html = html
        self.check_dir = check_dir

    def __call__(self, scope: Any, receive: Any, send: Any):  # pragma: no cover - compatibility only
        raise RuntimeError("Static file serving is not available in the FastAPI stub")
