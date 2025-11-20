"""SSE stream for pipeline events."""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from typing import Any, Dict, Optional

from starlette.responses import StreamingResponse

from .database import Database, list_pipeline_events

logger = logging.getLogger(__name__)

_active_pipeline_tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()


def _sse_headers() -> Dict[str, str]:
    return {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store, no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def pipeline_stream_response(
    request,
    db: Database,
    user: Dict[str, Any],
    *,
    job_id: Optional[str] = None,
) -> StreamingResponse:
    """SSE stream for pipeline events.
    
    Worker Compatibility:
    - Works with Cloudflare Workers ASGI adapter
    - Uses async generators with polling (2s interval)
    - Long-running connections may hit Worker timeout limits (30s default, 300s max)
    - Monitor timeout behavior in production; may need polling fallback for very long connections
    """
    user_id = user.get("user_id")
    if not user_id:
        raise ValueError("Missing user_id for pipeline stream")

    async def event_generator():
        last_sequence: Optional[int] = None
        task = asyncio.current_task()
        if task:
            _active_pipeline_tasks.add(task)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    events = await asyncio.wait_for(
                        list_pipeline_events(
                            db,
                            user_id,
                            job_id=job_id,
                            after_sequence=last_sequence,
                            limit=25,
                        ),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "pipeline_stream list_pipeline_events timed out",
                        extra={
                            "user_id": user_id,
                            "job_id": job_id,
                            "after_sequence": last_sequence,
                        },
                    )
                    events = []
                if events:
                    for event in events:
                        seq = event.get("sequence")
                        if isinstance(seq, int):
                            last_sequence = seq
                        else:
                            logger.debug(
                                "pipeline_stream: skipping event with invalid sequence",
                                extra={
                                    "user_id": user_id,
                                    "job_id": job_id,
                                    "sequence": seq,
                                },
                            )
                        payload = json.dumps({
                            "type": "pipeline.event",
                            "data": event,
                        })
                        yield f"data: {payload}\n\n"
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            logger.debug("Pipeline SSE cancelled during shutdown")
            raise
        except Exception:
            logger.exception("pipeline_stream event_generator failed")
            try:
                error_payload = json.dumps({
                    "type": "error",
                    "data": {"message": "Pipeline stream error occurred"},
                })
                yield f"data: {error_payload}\n\n"
            except Exception:
                logger.exception("Failed to emit pipeline SSE error event")
            raise
        finally:
            if task:
                _active_pipeline_tasks.discard(task)

    return StreamingResponse(event_generator(), headers=_sse_headers())


def cancel_all_pipeline_streams() -> int:
    cancelled = 0
    tasks = list(_active_pipeline_tasks)
    for task in tasks:
        if not task.done():
            task.cancel()
            cancelled += 1
    return cancelled
