"""Execute blocking Google client requests in a worker-friendly way."""

from __future__ import annotations

import asyncio
from typing import Any


async def execute_google_request(request: Any) -> Any:
    """Run a synchronous Google client request in a thread to avoid blocking."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, request.execute)


__all__ = ["execute_google_request"]
