"""Minimal googleapiclient.http stubs."""
from __future__ import annotations


class MediaIoBaseDownload:  # pragma: no cover - helper only
    def __init__(self, fh, request):
        self.fh = fh
        self.request = request

    def next_chunk(self):
        return None, True


class MediaIoBaseUpload:  # pragma: no cover - helper only
    def __init__(self, fh, mimetype: str):
        self.fh = fh
        self.mimetype = mimetype
