"""
Cloudflare Queue helpers.

Inline development mode (USE_INLINE_QUEUE=true) bypasses Cloudflare Queues entirely; the
worker polls the database for pending jobs. Production mode uses either the
Cloudflare Worker bindings or the public Queue HTTP API.
"""

from __future__ import annotations

import json
import logging
import datetime
from typing import Dict, Any, Optional, Protocol

from .simple_http import AsyncSimpleClient

from .config import settings

logger = logging.getLogger(__name__)


class QueueLike(Protocol):
    async def send(self, message: Dict[str, Any]) -> Any:
        ...


class CloudflareQueueAPI:
    """Thin wrapper around the Cloudflare Queue HTTP API."""

    def __init__(self, account_id: str, api_token: str, queue_name: str):
        self.account_id = account_id
        self.api_token = api_token
        self.queue_name = queue_name
        self._client: Optional[AsyncSimpleClient] = None

    @property
    def client(self) -> AsyncSimpleClient:
        if self._client is None:
            self._client = AsyncSimpleClient(timeout=10)
        return self._client

    @property
    def endpoint(self) -> str:
        return f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/queues/{self.queue_name}/messages"

    async def send(self, message: Dict[str, Any]) -> None:
        payload = {
            "body": json.dumps(message),
            "timestamp_ms": int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp() * 1000),
        }
        headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
        resp = await self.client.post(self.endpoint, json={"messages": [payload]}, headers=headers)
        if resp.status_code >= 300:
            logger.error(
                "Cloudflare Queue send failed; see docs/DEPLOYMENT.md#queue-configuration-modes",
                extra={
                    "status": resp.status_code,
                    "body": resp.text,
                    "doc_hint": "docs/DEPLOYMENT.md#queue-configuration-modes",
                    "queue_name": self.queue_name,
                },
            )
            resp.raise_for_status()

    async def close(self) -> None:
        if self._client is not None:
            client = self._client
            self._client = None
            try:
                # Prefer async close if available
                close = getattr(client, "aclose", None)
                if callable(close):
                    await close()
                else:
                    close_sync = getattr(client, "close", None)
                    if callable(close_sync):
                        close_sync()
            except Exception:
                logger.warning("Error while closing CloudflareQueueAPI HTTP client", exc_info=True)


class QueueProducer:
    """Unified queue producer supporting inline + Cloudflare modes."""

    def __init__(self, queue: Optional[QueueLike] = None, dlq: Optional[QueueLike] = None):
        self._queue: Optional[QueueLike] = queue
        self._dlq: Optional[QueueLike] = dlq
        self._cf_queue_api: Optional[CloudflareQueueAPI] = None
        self._cf_dlq_api: Optional[CloudflareQueueAPI] = None
        self._inline_mode = settings.use_inline_queue
        self._initialized = False

    def _init_primary_queue(self) -> None:
        """Initialize primary job queue from bindings or HTTP API if needed."""
        if self._queue is not None:
            return

        if settings.queue is not None:
            self._queue = settings.queue
            logger.info("Using Cloudflare Workers queue binding")
            return

        if settings.cloudflare_api_token and settings.cf_queue_name:
            if settings.cloudflare_account_id:
                self._cf_queue_api = CloudflareQueueAPI(
                    account_id=settings.cloudflare_account_id,
                    api_token=settings.cloudflare_api_token,
                    queue_name=settings.cf_queue_name,
                )
                logger.info("Initialized Cloudflare Queue API client")
            else:
                logger.warning("cloudflare_account_id missing; skipping CloudflareQueueAPI initialization")
        else:
            logger.warning("No queue binding/API configured; jobs will remain pending")

    def _init_dlq(self) -> None:
        """Initialize dead-letter queue from bindings or HTTP API if needed."""
        if self._dlq is not None:
            return

        if settings.dlq is not None:
            self._dlq = settings.dlq
            return

        if settings.cloudflare_api_token and settings.cf_queue_dlq:
            if settings.cloudflare_account_id:
                self._cf_dlq_api = CloudflareQueueAPI(
                    account_id=settings.cloudflare_account_id,
                    api_token=settings.cloudflare_api_token,
                    queue_name=settings.cf_queue_dlq,
                )
            else:
                logger.warning("cloudflare_account_id missing; skipping Cloudflare DLQ API initialization")

    def _initialize(self) -> None:
        if self._initialized:
            return

        if self._inline_mode:
            logger.info("Queue producer running in inline mode (DB polling)")
        else:
            self._init_primary_queue()
            self._init_dlq()
        self._initialized = True

    @property
    def queue(self) -> Optional[QueueLike]:
        self._initialize()
        return self._queue

    @property
    def dlq(self) -> Optional[QueueLike]:
        self._initialize()
        return self._dlq

    async def _send_via_cloudflare(self, message: Dict[str, Any]) -> bool:
        if not self._cf_queue_api:
            return False
        await self._cf_queue_api.send(message)
        return True

    async def send_generic(self, message: Dict[str, Any]) -> bool:
        """Validate and dispatch a message."""
        self._initialize()

        if self._inline_mode:
            # Inline mode relies on jobs.payload + DB polling.
            logger.debug("Inline queue mode: skipping external enqueue", extra={"job_id": message.get("job_id")})
            return True

        if not self.queue and not self._cf_queue_api:
            logger.warning(
                "Queue not configured; message will not be processed (docs/DEPLOYMENT.md#queue-configuration-modes)",
                extra={"doc_hint": "docs/DEPLOYMENT.md#queue-configuration-modes", "job_id": message.get("job_id")},
            )
            return False

        def _is_str(value: Any) -> bool:
            return isinstance(value, str) and bool(value.strip())

        if "job_type" in message:
            required = [_is_str(message.get("job_id")), _is_str(message.get("user_id")), _is_str(message.get("job_type"))]
            if not all(required):
                logger.error("Invalid job message: missing job_id/user_id/job_type")
                return False
            jt = str(message["job_type"])
            if jt in {"ingest_youtube", "optimize_drive", "generate_blog"} and not _is_str(message.get("document_id")):
                logger.error("Invalid job message: document_id required", extra={"job_type": jt})
                return False
            if jt == "ingest_youtube" and not _is_str(message.get("youtube_video_id")):
                logger.error("Invalid ingest_youtube message: missing youtube_video_id")
                return False
        elif "operation" in message:
            if not (_is_str(message.get("document_id")) and _is_str(message.get("operation"))):
                logger.error("Invalid document operation message: require document_id and operation")
                return False
        else:
            logger.error("Unknown message shape; rejecting generic send")
            return False

        try:
            if self.queue is not None:
                # Cloudflare's Queue binding lives on the JS side; passing a raw
                # Python dict through Pyodide can trigger DataCloneError. To
                # avoid this, serialize the message to JSON and send a string.
                import json as _json  # local import to avoid polluting module namespace
                payload = _json.dumps(message)
                await self.queue.send(payload)
            else:
                await self._send_via_cloudflare(message)
        except Exception:
            logger.error(
                "Failed to send queue message; see docs/DEPLOYMENT.md#queue-configuration-modes",
                exc_info=True,
                extra={"job_id": message.get("job_id"), "doc_hint": "docs/DEPLOYMENT.md#queue-configuration-modes"},
            )
            raise
        else:
            logger.info("Queue message enqueued", extra={"job_id": message.get("job_id")})
            return True

    async def send_to_dlq(self, job_id: str, error: str, original_message: Dict[str, Any]) -> bool:
        if self._inline_mode:
            logger.info("Inline mode DLQ noop", extra={"job_id": job_id, "error": error})
            return True

        if not self.dlq and not self._cf_dlq_api:
            logger.warning("DLQ not configured")
            return False

        message = {
            "job_id": job_id,
            "error": error,
            "original_message": original_message,
            "failed_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        try:
            if self.dlq is not None:
                await self.dlq.send(message)
            elif self._cf_dlq_api is not None:
                await self._cf_dlq_api.send(message)
            logger.info("Sent job to DLQ", extra={"job_id": job_id})
            return True
        except Exception:
            logger.error("Failed to send job to DLQ", exc_info=True, extra={"job_id": job_id})
            return False

    async def close(self) -> None:
        if self._cf_queue_api:
            await self._cf_queue_api.close()
        if self._cf_dlq_api:
            await self._cf_dlq_api.close()
