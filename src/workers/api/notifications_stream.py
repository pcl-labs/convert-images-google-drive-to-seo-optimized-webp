from starlette.responses import StreamingResponse
from typing import Optional, Dict, Any
import json
import asyncio
import logging
import weakref

from .database import list_notifications, Database

logger = logging.getLogger(__name__)

# Track active SSE connections for graceful shutdown
_active_sse_tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()


def _sse_headers() -> Dict[str, str]:
    return {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store, no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def notifications_stream_response(request, db: Database, user: Dict[str, Any]) -> StreamingResponse:
    user_id = user.get("user_id")
    if not user_id:
        logger.warning("notifications_stream_response called without user_id in user context: %s", user)
        raise ValueError("Missing user_id in user context for notifications stream")

    async def event_generator():
        last_sent: Optional[str] = None
        task = asyncio.current_task()
        if task:
            _active_sse_tasks.add(task)
        try:
            while True:
                if await request.is_disconnected():
                    break
                notifs = await list_notifications(db, user_id, after_id=last_sent, limit=20)
                if notifs:
                    for n in notifs:
                        notification_id = n.get("id")
                        if not notification_id:
                            logger.warning(f"Notification missing ID field, skipping: {n}")
                            continue
                        last_sent = notification_id
                        context_payload = n.get("context")
                        if isinstance(context_payload, str):
                            try:
                                context_payload = json.loads(context_payload)
                            except Exception:
                                context_payload = {}
                        payload = json.dumps({
                            "type": "notification.created",
                            "data": {
                                "id": notification_id,
                                "level": n.get("level"),
                                "text": n.get("text"),
                                "title": n.get("title"),
                                "created_at": n.get("created_at"),
                                "context": context_payload or {},
                            },
                        })
                        yield f"data: {payload}\n\n"
                else:
                    # heartbeat comment to keep connection alive
                    yield ": heartbeat\n\n"
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            logger.debug("SSE connection cancelled during shutdown")
            raise
        except Exception:
            logger.exception("notifications_stream event_generator failed")
            # Emit error event to client before re-raising
            try:
                error_payload = json.dumps({
                    "type": "error",
                    "data": {
                        "message": "Notification stream error occurred",
                    },
                })
                yield f"data: {error_payload}\n\n"
            except Exception:
                # If we can't send error event, log and continue to re-raise
                logger.exception("Failed to emit SSE error event")
            raise
        finally:
            if task:
                _active_sse_tasks.discard(task)

    return StreamingResponse(event_generator(), headers=_sse_headers())


async def cancel_all_sse_connections() -> int:
    """Cancel all active SSE connections. Returns number of cancelled connections."""
    if not _active_sse_tasks:
        return 0
    
    # Create a list copy since we'll be modifying the set
    tasks_to_cancel = list(_active_sse_tasks)
    cancelled_count = 0
    
    for task in tasks_to_cancel:
        if not task.done():
            task.cancel()
            cancelled_count += 1
    
    # Wait briefly for tasks to finish cancelling
    if tasks_to_cancel:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                timeout=1.0
            )
        except asyncio.TimeoutError:
            logger.warning(f"Some SSE connections did not cancel within timeout")
        except Exception as e:
            logger.warning(f"Error waiting for SSE connections to cancel: {e}")
    
    logger.info(f"Cancelled {cancelled_count} active SSE connections")
    return cancelled_count
