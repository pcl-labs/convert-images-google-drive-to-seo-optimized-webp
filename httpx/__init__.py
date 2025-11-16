"""Minimal httpx compatibility layer for offline tests."""
from __future__ import annotations

from typing import Any, Optional


class RequestError(Exception):
    pass


class HTTPStatusError(RequestError):
    def __init__(self, message: str = "HTTP error", request: Any | None = None, response: Any | None = None):
        super().__init__(message)
        self.request = request
        self.response = response


class Response:
    def __init__(self, status_code: int = 200, text: str = "", json_data: Any | None = None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data if json_data is not None else {}

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPStatusError("HTTP status error", response=self)


class AsyncClient:
    def __init__(self, *args: Any, **kwargs: Any):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
        return False

    async def get(self, *args: Any, **kwargs: Any) -> Response:
        raise RequestError("httpx stub cannot perform network requests")

    async def post(self, *args: Any, **kwargs: Any) -> Response:
        raise RequestError("httpx stub cannot perform network requests")

    async def aclose(self) -> None:
        return None
