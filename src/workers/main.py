"""
Cloudflare Worker entrypoint that forwards requests into the shared FastAPI app.
"""

from __future__ import annotations

from workers import WorkerEntrypoint

# Lazily initialize the FastAPI app the first time a request hits the worker.
fastapi_app = None


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        global fastapi_app
        
        # Import inside the handler to avoid startup CPU limit
        from workers.runtime import apply_worker_env
        from api.app_factory import create_app
        from workers.asgi_adapter import handle_worker_request
        
        apply_worker_env(self.env)
        if fastapi_app is None:
            fastapi_app = create_app()
        return await handle_worker_request(fastapi_app, request, self.env, self.ctx)
    
    async def queue(self, batch, env):
        """Handle queue messages from Cloudflare Queues."""
        # Import inside the handler to avoid startup CPU limit
        from workers.runtime import apply_worker_env
        from api.database import Database
        from workers.consumer import handle_queue_message
        
        apply_worker_env(env)
        db = Database(db=env.DB)
        
        # Process each message in the batch
        for message in batch.messages:
            try:
                await handle_queue_message(message.body, db)
                message.ack()
            except Exception as e:
                # Log error and retry (Cloudflare will handle retries based on config)
                print(f"Error processing queue message: {e}")
                message.retry()
