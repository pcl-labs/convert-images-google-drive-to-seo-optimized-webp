"""
Application factory for building the FastAPI app in both local (Uvicorn)
and Cloudflare Worker environments.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Optional, Set, Awaitable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .config import Settings, settings as global_settings
from .cloudflare_queue import QueueProducer
from .database import Database, ensure_notifications_schema
from .exceptions import APIException
from .app_logging import setup_logging, get_logger, get_request_id
from .middleware import (
    AuthCookieMiddleware,
    CORSMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)
from .deps import set_db_instance, set_queue_producer
from .notifications_stream import cancel_all_sse_connections


def create_app(custom_settings: Optional[Settings] = None) -> FastAPI:
    """
    Build the FastAPI ASGI application.

    Args:
        custom_settings: Optional Settings instance. If omitted, the global
            Settings object that is shared across modules will be used.
    """

    active_settings = custom_settings or global_settings
    log_level = "INFO" if not active_settings.debug else "DEBUG"
    setup_logging(level=log_level, use_json=True)
    app_logger = get_logger(__name__)

    # Store references on the closure so each application instance keeps its
    # own Database / Queue state. This prevents Worker environments from
    # leaking state across isolates.
    db_instance: Optional[Database] = None
    queue_producer: Optional[QueueProducer] = None
    app_tasks: Set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Lifespan context manager for startup/shutdown."""
        nonlocal db_instance, queue_producer

        app_logger.info("Starting application")

        # Database bindings differ between local (sqlite/postgres) and Workers
        # (D1 binding). Database() abstracts that difference.
        db_instance = Database(db=active_settings.d1_database)
        app_logger.info("Database initialized")
        set_db_instance(db_instance)

        try:
            await ensure_notifications_schema(db_instance)
            app_logger.info("Notifications schema ensured")
        except Exception as exc:  # pragma: no cover - defensive logging path
            app_logger.warning("Failed ensuring notifications schema: %s", exc)

        queue_producer = QueueProducer(queue=active_settings.queue, dlq=active_settings.dlq)
        queue_mode = "inline" if active_settings.use_inline_queue else (
            "workers-binding" if active_settings.queue else "api"
        )
        app_logger.info("Queue producer initialized (mode: %s)", queue_mode)
        set_queue_producer(queue_producer)

        yield

        app_logger.info("Shutting down application")

        async def shutdown_cleanup():
            """Perform shutdown cleanup operations."""
            nonlocal db_instance, queue_producer

            try:
                sse_count = await cancel_all_sse_connections()
                if sse_count > 0:
                    app_logger.info("Cancelled %s SSE connections", sse_count)
            except Exception as exc:  # pragma: no cover - defensive logging
                app_logger.error("Error cancelling SSE connections: %s", exc, exc_info=True)

            try:
                current_task = asyncio.current_task()
                all_tasks = [
                    task for task in app_tasks
                    if task is not current_task and not task.done()
                ]

                if all_tasks:
                    app_logger.info("Cancelling %s background tasks", len(all_tasks))
                    for task in all_tasks:
                        task.cancel()

                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*all_tasks, return_exceptions=True),
                            timeout=2.0,
                        )
                        app_logger.info("All background tasks cancelled")
                    except asyncio.TimeoutError:
                        app_logger.warning("Background task cancellation timed out")
            except Exception as exc:  # pragma: no cover - defensive logging
                app_logger.error("Error cancelling background tasks: %s", exc, exc_info=True)

            if db_instance is not None:
                try:
                    if hasattr(db_instance, "db") and db_instance.db is not None:
                        db_obj = db_instance.db
                        for method_name in ["close", "disconnect", "cleanup"]:
                            if hasattr(db_obj, method_name):
                                method = getattr(db_obj, method_name)
                                if inspect.iscoroutinefunction(method):
                                    await method()
                                else:
                                    method()
                                app_logger.info("Database %s called", method_name)
                                break
                    app_logger.info("Database connection closed")
                except Exception as exc:  # pragma: no cover - defensive logging
                    app_logger.error("Error closing database connection: %s", exc, exc_info=True)
                finally:
                    db_instance = None

            if queue_producer is not None:
                if hasattr(queue_producer, "close"):
                    try:
                        result = queue_producer.close()
                        if inspect.isawaitable(result) or asyncio.iscoroutine(result):
                            await result
                        app_logger.info("Queue producer HTTP clients closed")
                    except Exception as exc:  # pragma: no cover
                        app_logger.error("Error closing queue producer: %s", exc, exc_info=True)

                try:
                    if hasattr(queue_producer, "queue") and queue_producer.queue is not None:
                        queue_obj = queue_producer.queue
                        for method_name in ["close", "stop", "cleanup", "shutdown"]:
                            if hasattr(queue_obj, method_name):
                                method = getattr(queue_obj, method_name)
                                if inspect.iscoroutinefunction(method):
                                    await method()
                                else:
                                    method()
                                app_logger.info("Queue %s called", method_name)
                                break
                except Exception as exc:  # pragma: no cover
                    app_logger.error("Error cleaning up queue object: %s", exc, exc_info=True)
                finally:
                    queue_producer = None
                    app_logger.info("Queue producer closed")

            app_logger.info("Shutdown cleanup completed")

        try:
            await asyncio.wait_for(shutdown_cleanup(), timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover - defensive logging
            app_logger.error("Shutdown cleanup timed out after 5 seconds")
        except Exception as exc:  # pragma: no cover
            app_logger.error("Unexpected error during shutdown: %s", exc, exc_info=True)

    app = FastAPI(
        title=active_settings.app_name,
        description="Production-ready API for optimizing images from Google Drive to WebP format",
        version=active_settings.app_version,
        lifespan=lifespan,
    )

    def register_background_task(coro: Awaitable) -> asyncio.Task:
        """Create and register a background task tied to this app instance."""
        task = asyncio.create_task(coro)
        app_tasks.add(task)
        task.add_done_callback(app_tasks.discard)
        return task

    app.state.register_background_task = register_background_task

    module_dir = Path(__file__).resolve().parent
    project_root = module_dir.parent.parent.parent
    static_dir = project_root / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from .web import router as web_router
    from .public import router as public_router
    from .protected import router as protected_router
    from .steps import router as steps_router

    app.include_router(web_router)
    app.include_router(public_router)
    app.include_router(protected_router)
    app.include_router(steps_router)

    @app.exception_handler(APIException)
    async def api_exception_handler(request, exc):  # pragma: no cover - FastAPI wiring
        extra = {"request_id": get_request_id()}
        app_logger.warning(
            "APIException handled",
            extra={
                **extra,
                "status_code": exc.status_code,
                "detail": exc.detail,
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "request_id": extra["request_id"]},
        )

    # Shared middleware stack for local + Worker runtimes.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AuthCookieMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        max_per_minute=active_settings.rate_limit_per_minute,
        max_per_hour=active_settings.rate_limit_per_hour,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIDMiddleware)

    return app


__all__ = ["create_app"]
