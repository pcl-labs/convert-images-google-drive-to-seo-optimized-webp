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
import logging

from runtime import apply_worker_env

# Lazily initialize the FastAPI app the first time a request hits the worker
app = None
logger = logging.getLogger(__name__)


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
    
    async def queue(self, batch, env, ctx):
        """Handle queue messages from Cloudflare Queues."""
        # 1) Apply Worker env → os.environ before importing anything that
        #    touches api.config.settings, mirroring the fetch() path ordering.
        from runtime import apply_worker_env

        # Use self.env here, just like fetch(), because that is where
        # Cloudflare exposes Worker-wide secrets (including JWT_SECRET_KEY).
        # The per-event env argument may not include all secrets.
        apply_worker_env(self.env)

        # 2) Now that Settings.from_env can see JWT_SECRET_KEY and other
        #    secrets via os.environ, it is safe to import modules that access
        #    api.config.settings at import time.
        from api.database import Database
        from api.config import settings
        from api.cloudflare_queue import QueueProducer
        import json as _json
        
        # D1 binding may not always be present on env for queue events in
        # certain runtimes; fall back to settings.d1_database if needed.
        db_binding = None
        if env is not None and hasattr(env, "DB"):
            db_binding = env.DB
        else:
            db_binding = settings.d1_database

        db = Database(db=db_binding)
        queue_producer = QueueProducer(queue=settings.queue, dlq=settings.dlq)
        
        # Process each message in the batch
        # Note: Most job processors have been removed (Drive, YouTube OAuth, content generation)
        # Only optimization jobs remain, which may also be removed in the future
        for message in batch.messages:
            try:
                body = message.body
                # Messages sent via the Python QueueProducer are JSON strings
                # when using the JS Queue binding. Decode back into a dict so
                # consumer.handle_queue_message always sees a Python mapping.
                if isinstance(body, str):
                    try:
                        parsed = _json.loads(body)
                    except Exception:
                        parsed = {"raw_body": body}
                    payload = parsed
                else:
                    payload = body

                # Stub: Most job processors removed
                job_type = payload.get("job_type", "unknown")
                logger.warning(
                    "Queue message received but job processor removed",
                    extra={"job_type": job_type, "job_id": payload.get("job_id")}
                )
                # Acknowledge to prevent retries for removed job types
                message.ack()
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
        return await self.queue(batch, env, ctx)
