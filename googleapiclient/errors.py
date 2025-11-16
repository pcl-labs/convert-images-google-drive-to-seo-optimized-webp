"""Minimal HttpError stub."""
from __future__ import annotations


class HttpError(Exception):
    def __init__(self, resp=None, content=None):
        super().__init__("HTTP error")
        self.resp = resp
        self.content = content
