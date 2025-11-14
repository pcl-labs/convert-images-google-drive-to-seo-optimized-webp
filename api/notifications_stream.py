from starlette.responses import StreamingResponse
from typing import Optional, Dict, Any
import json
import asyncio
import logging

from .database import list_notifications, Database

logger = logging.getLogger(__name__)


def _sse_headers() -> Dict[str, str]:
    return {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store, no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def notifications_stream_response(request, db: Database, user: Dict[str, Any]) -> StreamingResponse:
    async def event_generator():
        last_sent: Optional[str] = None
        try:
            while True:
                if await request.is_disconnected():
                    break
                notifs = await list_notifications(db, user["user_id"], after_id=last_sent, limit=20)
                if notifs:
                    for n in notifs:
                        if "id" in n:
                            last_sent = n["id"]
                        payload = json.dumps({
                            "type": "notification.created",
                            "data": {
                                "id": n.get("id"),
                                "level": n.get("level"),
                                "text": n.get("text"),
                                "created_at": n.get("created_at"),
                            },
                        })
                        yield f"data: {payload}\n\n"
                else:
                    # heartbeat comment to keep connection alive
                    yield ": heartbeat\n\n"
                await asyncio.sleep(5)
        except Exception:
            logger.exception("notifications_stream event_generator failed")
            return

    return StreamingResponse(event_generator(), headers=_sse_headers())
