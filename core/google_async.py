"""Async helpers for executing Google API requests without blocking the event loop."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httplib2
from google_auth_httplib2 import AuthorizedHttp  # type: ignore


def _threadsafe_execute(request: Any) -> Any:
    """Execute a googleapiclient request with a per-call AuthorizedHttp."""
    http = getattr(request, "http", None)
    new_http: Optional[AuthorizedHttp] = None
    credentials = getattr(http, "credentials", None)
    if credentials is not None:
        new_http = AuthorizedHttp(credentials, http=httplib2.Http())
    if new_http is not None:
        return request.execute(http=new_http)
    return request.execute()


async def execute_google_request(request: Any) -> Any:
    """Run a googleapiclient request.execute() call in a thread using a fresh HTTP client."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _threadsafe_execute, request)
