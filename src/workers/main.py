"""
Cloudflare Worker entrypoint that forwards requests into the shared FastAPI app.
"""

from __future__ import annotations

import asyncio
import logging

from workers import WorkerEntrypoint

logger = logging.getLogger(__name__)

# Lazily initialize the FastAPI app the first time a request hits the worker.
fastapi_app = None
_app_lock: asyncio.Lock | None = None
_app_init_error: Exception | None = None


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        global fastapi_app, _app_init_error, _app_lock
        
        # Import inside the handler to avoid startup CPU limit
        from .runtime import apply_worker_env
        from api.app_factory import create_app
        from .asgi_adapter import handle_worker_request
        
        apply_worker_env(self.env)

        if _app_lock is None:
            _app_lock = asyncio.Lock()

        if fastapi_app is None and _app_init_error is None:
            async with _app_lock:
                if fastapi_app is None and _app_init_error is None:
                    try:
                        fastapi_app = create_app()
                    except Exception as exc:
                        logger.exception(
                            "Failed to initialize FastAPI app in worker",
                            exc_info=True,
                        )
                        _app_init_error = exc
                        raise

        if _app_init_error is not None:
            # Re-raise the initialization error for subsequent requests
            raise _app_init_error.with_traceback(_app_init_error.__traceback__)

        return await handle_worker_request(fastapi_app, request, self.env, self.ctx)
    
    async def queue(self, batch, env):
        """Handle queue messages from Cloudflare Queues."""
        # Import inside the handler to avoid startup CPU limit
        from .runtime import apply_worker_env
        from api.database import Database
        from workers.consumer import handle_queue_message
        
        apply_worker_env(env)
        db = Database(db=env.DB)
        
        # Process each message in the batch
        for message in batch.messages:
            try:
                await handle_queue_message(message.body, db)
            except Exception:
                logger.exception(
                    "Error processing queue message",
                    extra={"queue_message_id": getattr(message, "id", None)},
                )
                message.retry()
                continue

            # Only acknowledge after successful processing; if ack fails, log but do not retry
            try:
                message.ack()
            except Exception:
                logger.exception(
                    "Error acknowledging queue message",
                    extra={"queue_message_id": getattr(message, "id", None)},
                )
