"""Minimal Pillow stub for offline tests."""
from __future__ import annotations


class _ImageModule:
    LANCZOS = 1

    class _Image:
        def convert(self, mode):
            return self

        def resize(self, size, resample=None):
            return self

        def save(self, fp, format=None, quality=None):
            raise NotImplementedError("Pillow stub cannot process images")

    def open(self, path):  # pragma: no cover - helper only
        raise NotImplementedError("Pillow stub cannot open images")


Image = _ImageModule()

