"""
Cloudflare Worker entrypoint that forwards requests into the shared FastAPI app.
"""

from __future__ import annotations

from api.app_factory import create_app
from workers.asgi_adapter import handle_worker_request
from workers.runtime import apply_worker_env

# Lazily initialize the FastAPI app the first time a request hits the worker.
fastapi_app = None


async def main(request, env, ctx):
    global fastapi_app
    apply_worker_env(env)
    if fastapi_app is None:
        fastapi_app = create_app()
    return await handle_worker_request(fastapi_app, request, env, ctx)


export = {"default": main}
