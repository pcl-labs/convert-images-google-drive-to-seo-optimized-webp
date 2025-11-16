"""Minimal discovery.build stub."""
from __future__ import annotations

from typing import Any


def build(serviceName: str, version: str, credentials=None, cache_discovery: bool | None = None) -> Any:  # pragma: no cover
    raise NotImplementedError("googleapiclient.discovery.build is not implemented in this environment")
