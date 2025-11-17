"""Execute blocking Google client requests in a worker-friendly way."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any


logger = logging.getLogger(__name__)


def _request_context(request: Any) -> dict[str, Any]:
    method = getattr(request, "_method", None) or getattr(request, "method", None)
    path = getattr(request, "_path", None) or getattr(request, "uri", None)
    return {"method": method or "execute", "path": path}


def _should_retry(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in {429}:
        return True
    if isinstance(status, int) and 500 <= status < 600:
        return True
    return isinstance(exc, (OSError, ConnectionError))


async def execute_google_request(request: Any, *, max_attempts: int = 3) -> Any:
    """Run a synchronous Google client request in a thread with retries."""
    execute_attr = getattr(request, "execute", None)
    if execute_attr is None or not callable(execute_attr):
        raise TypeError("request must expose a callable execute() method")

    loop = asyncio.get_running_loop()
    context = _request_context(request)
    delay = 0.2

    for attempt in range(1, max_attempts + 1):
        try:
            return await loop.run_in_executor(None, request.execute)
        except Exception as exc:
            logger.warning(
                "Google request failed",
                extra={"attempt": attempt, "max_attempts": max_attempts, **context},
                exc_info=True,
            )
            if attempt >= max_attempts or not _should_retry(exc):
                logger.error(
                    "Google request giving up",
                    extra={"attempt": attempt, **context},
                )
                raise
            await asyncio.sleep(delay + random.uniform(0, 0.1))
            delay *= 2


__all__ = ["execute_google_request"]
