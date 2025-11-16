"""Async helpers for executing Google API requests without blocking the event loop."""

from __future__ import annotations

import asyncio
from typing import Any


async def execute_google_request(request: Any) -> Any:
    """Run a googleapiclient request.execute() call in a thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, request.execute)
