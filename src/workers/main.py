"""
Cloudflare Worker entrypoint that forwards requests into the shared FastAPI app.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src/workers to Python path for Cloudflare Workers
# This allows imports to work when main.py is executed directly
_workers_path = Path(__file__).parent
if str(_workers_path) not in sys.path:
    sys.path.insert(0, str(_workers_path))

from workers import WorkerEntrypoint
import asgi

from runtime import apply_worker_env

# Lazily initialize the FastAPI app the first time a request hits the worker
app = None


class Default(WorkerEntrypoint):
    """
    Cloudflare Python Worker entrypoint.
    
    This class is the main entrypoint for the Cloudflare Worker runtime.
    It handles HTTP requests by forwarding them to the FastAPI app via
    Cloudflare's built-in ASGI server.
    
    The FastAPI app is lazily initialized on the first request to avoid
    hitting Cloudflare's startup CPU limit.
    """
    async def fetch(self, request):
        global app
        
        # 1) Apply Worker env → os.environ before importing/using config
        # This ensures JWT_SECRET_KEY and other secrets are in os.environ
        # before config.py evaluates Settings.from_env() at import time
        apply_worker_env(self.env)
        
        # 2) Import app_factory AFTER env is applied (avoids config.py reading env too early)
        from api.app_factory import create_app
        from api.asgi_safe import SingleResponseMiddleware
        
        # 3) Lazily create FastAPI app once per isolate
        if app is None:
            inner_app = create_app()
            # Wrap with SingleResponseMiddleware to prevent InvalidStateError
            # This ensures only one complete response per request, ignoring
            # Starlette's error recovery attempts that would cause InvalidStateError
            app = SingleResponseMiddleware(inner_app)
        
        # 4) Delegate to Cloudflare's ASGI server
        return await asgi.fetch(app, request, self.env)

    async def on_fetch(self, request, env, ctx):
        """Cloudflare Python Worker entrypoint for fetch events.

        This method is discovered by the Workers runtime; delegate to the
        existing fetch() implementation.
        """
        return await self.fetch(request)
    
    async def queue(self, batch, env):
        """Handle queue messages from Cloudflare Queues."""
        # Import inside the handler to avoid startup CPU limit
        from runtime import apply_worker_env
        from api.database import Database
        from consumer import handle_queue_message
        from api.config import settings
        from api.cloudflare_queue import QueueProducer
        
        apply_worker_env(env)
        db = Database(db=env.DB)
        queue_producer = QueueProducer(queue=settings.queue, dlq=settings.dlq)
        
        # Process each message in the batch
        for message in batch.messages:
            try:
                await handle_queue_message(message.body, db, queue_producer)
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

    async def on_queue(self, batch, env, ctx):
        """Cloudflare Python Worker entrypoint for queue events.

        Delegate to the existing queue() implementation.
        """
        return await self.queue(batch, env)
