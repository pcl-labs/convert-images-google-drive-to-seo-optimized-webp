"""
Application factory for building the FastAPI app.

This factory is used in both environments:
- Local Development: Called by src/workers/api/main.py, which is imported by
  run_api.py (Uvicorn server). Uvicorn is NOT used in the Worker runtime.
- Cloudflare Worker: Called by src/workers/main.py (WorkerEntrypoint), which
  uses Cloudflare's built-in `asgi` module to handle requests without Uvicorn.

The same FastAPI app instance works in both environments via the ASGI interface.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Optional, Set, Awaitable

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from pathlib import Path
from .static_loader import mount_static_files

from .config import Settings, settings as global_settings
from .cloudflare_queue import QueueProducer
from .database import Database, ensure_sessions_schema, ensure_full_schema
from .exceptions import APIException
from .app_logging import setup_logging, get_logger, get_request_id
from .middleware import (
    CORSMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)
from .deps import set_db_instance, set_queue_producer


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
    
    # Log startup info for debugging
    app_logger.info(
        "Starting application: environment=%s, debug=%s, log_level=%s",
        active_settings.environment,
        active_settings.debug,
        log_level,
    )

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
            await ensure_sessions_schema(db_instance)
            app_logger.info("Session schema ensured")
            # Apply full schema to ensure all tables exist
            await ensure_full_schema(db_instance)
            app_logger.info("Full database schema ensured")
        except Exception as exc:  # pragma: no cover - fail fast on schema errors
            app_logger.error(
                "Failed ensuring database schema: %s, error_type=%s",
                str(exc),
                type(exc).__name__,
                exc_info=True,
            )
            # Fail fast - re-raise exception to stop startup
            raise

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

            # Removed: SSE connection cleanup - notifications stream removed

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
        docs_url=None,
        redoc_url=None,
    )

    def register_background_task(coro: Awaitable) -> asyncio.Task:
        """Create and register a background task tied to this app instance."""
        task = asyncio.create_task(coro)
        app_tasks.add(task)
        task.add_done_callback(app_tasks.discard)
        return task

    app.state.register_background_task = register_background_task

    # Mount static files using package-based loader that works in both
    # local dev and Cloudflare Workers environments
    mount_static_files(
        app, 
        static_dir_setting=active_settings.static_files_dir,
        assets_binding=active_settings.assets
    )

    from fastapi.openapi.docs import get_swagger_ui_html
    from fastapi.responses import HTMLResponse
    from .public import router as public_router
    from .protected import router as protected_router
    from .proxy import router as proxy_router

    app.include_router(public_router)
    app.include_router(protected_router)
    app.include_router(proxy_router)

    @app.get("/", include_in_schema=False)
    async def custom_swagger_ui():
        """Serve Swagger UI with handy auth buttons."""
        swagger_response = get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Docs",
        )
        html = swagger_response.body.decode("utf-8")
        auth_html = """
        <div id="auth-shortcuts">
            <h3>Authentication Shortcuts</h3>
            <p>Use these to log in and create API keys for trying endpoints directly in Swagger.</p>
            <div class="auth-buttons">
                <a class="auth-btn" href="/auth/github/start" target="_blank" rel="noopener">GitHub OAuth Login</a>
                <a class="auth-btn" href="/auth/google/start" target="_blank" rel="noopener">Google OAuth Login</a>
                <form action="/auth/keys" method="post" target="_blank">
                    <button type="submit" class="auth-btn">Create API Key</button>
                </form>
            </div>
        </div>
        <style>
            #auth-shortcuts {
                background: #0b0c10;
                color: #fff;
                padding: 16px;
                margin: 0;
                border-bottom: 1px solid rgba(255,255,255,0.1);
            }
            #auth-shortcuts h3 {
                margin: 0 0 8px 0;
                font-size: 1.1rem;
            }
            #auth-shortcuts p {
                margin: 0 0 12px 0;
                font-size: 0.95rem;
            }
            #auth-shortcuts .auth-buttons {
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
            }
            #auth-shortcuts .auth-buttons form {
                margin: 0;
            }
            #auth-shortcuts .auth-btn {
                background: #00bcd4;
                color: #0b0c10;
                padding: 8px 14px;
                border-radius: 4px;
                text-decoration: none;
                font-weight: 600;
            }
            #auth-shortcuts .auth-btn:hover {
                background: #0097a7;
            }
        </style>
        """
        html = html.replace("<body>", f"<body>{auth_html}", 1)
        return HTMLResponse(
            content=html,
            status_code=swagger_response.status_code,
            headers=dict(swagger_response.headers.items()),
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        """Serve favicon.ico from the static assets.

        Browsers automatically request /favicon.ico even if not referenced in HTML.
        Redirect to the static-served icon so this endpoint works in both local
        development and the Cloudflare Workers static loader.
        """
        return RedirectResponse(url="/static/favicon.ico")

    # Register exception handlers - Exception first (catch-all), then APIException (more specific)
    # FastAPI processes handlers in reverse registration order, so Exception handler will catch
    # everything except APIException (which gets re-raised)
    @app.exception_handler(Exception)
    async def global_exception_handler(request, exc):  # pragma: no cover - FastAPI wiring
        """Catch all unhandled exceptions and log them.
        
        This handler logs all exceptions for debugging, especially useful in Cloudflare Workers
        where logs are visible in Wrangler output and the Cloudflare dashboard.
        """
        # Re-raise APIException to use its specific handler
        if isinstance(exc, APIException):
            raise
        
        request_id = get_request_id()
        app_logger.error(
            "Unhandled exception: path=%s, method=%s, error=%s, error_type=%s",
            request.url.path,
            request.method,
            str(exc),
            type(exc).__name__,
            exc_info=True,
            extra={
                "request_id": request_id,
                "path": request.url.path,
                "method": request.method,
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "detail": str(exc) if active_settings.debug else "An error occurred",
                "error_type": type(exc).__name__,
                "request_id": request_id,
            }
        )
    
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
    # Note: Middleware executes in REVERSE order of registration.
    # Register AuthCookieMiddleware before SessionMiddleware so SessionMiddleware executes first
    # (SessionMiddleware sets request.state.session_user_id, AuthCookieMiddleware reads it)
    # Re-enabling AuthCookieMiddleware - core auth functionality
    # Re-enabling SessionMiddleware - D1 is now working
    # Sessions are used for stateful tracking (notifications, activity) and can help with OAuth flows
    # FlashMiddleware re-enabled - testing fix for ASGI errors (moved DB write to after call_next)
    
    # RateLimitMiddleware disabled - uses time.monotonic() and asyncio.Lock() which may not work correctly in Workers
    # To re-enable: implement using Cloudflare KV or Workers KV for distributed rate limiting
    # app.add_middleware(
    #     RateLimitMiddleware,
    #     max_per_minute=active_settings.rate_limit_per_minute,
    #     max_per_hour=active_settings.rate_limit_per_hour,
    # )
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
