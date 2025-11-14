"""
Production-ready FastAPI web application for Google Drive Image Optimizer.
"""

from fastapi import FastAPI, Request, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from typing import Optional
import inspect
 

from .config import settings
 

from .database import (
    Database,
    ensure_notifications_schema,
)
from .cloudflare_queue import QueueProducer
from .exceptions import (
    APIException,
)
from .app_logging import setup_logging, get_logger, get_request_id
from .middleware import (
    RequestIDMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    CORSMiddleware
)
from .deps import (
    set_db_instance,
    set_queue_producer,
)

# Set up logging
logger = setup_logging(level="INFO" if not settings.debug else "DEBUG", use_json=True)
app_logger = get_logger(__name__)


# Database and queue instances (will be bound at runtime)
db_instance: Optional[Database] = None
queue_producer: Optional[QueueProducer] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global db_instance, queue_producer
    
    # Startup
    app_logger.info("Starting application")
    
    # Initialize database
    db_instance = Database(db=settings.d1_database)
    app_logger.info("Database initialized")
    # Expose to shared deps
    set_db_instance(db_instance)
    # Ensure notifications schema exists
    try:
        await ensure_notifications_schema(db_instance)
        app_logger.info("Notifications schema ensured")
    except Exception as e:
        app_logger.warning(f"Failed ensuring notifications schema: {e}")
    
    # Initialize queue producer
    queue_producer = QueueProducer(queue=settings.queue, dlq=settings.dlq)
    app_logger.info("Queue producer initialized")
    # Expose to shared deps
    set_queue_producer(queue_producer)
    
    # Add authentication middleware after db is initialized
    # Note: This is a workaround - in production, middleware should be added before app creation
    # For now, we'll handle auth in dependencies instead
    
    yield
    
    # Shutdown
    app_logger.info("Shutting down application")
    
    # Cleanup database connection
    if db_instance is not None:
        try:
            # Check if the underlying db object has a close method
            if hasattr(db_instance, 'db') and db_instance.db is not None:
                db_obj = db_instance.db
                # Check for common close/disconnect method names
                for method_name in ['close', 'disconnect', 'cleanup']:
                    if hasattr(db_obj, method_name):
                        method = getattr(db_obj, method_name)
                        if inspect.iscoroutinefunction(method):
                            await method()
                        else:
                            method()
                        app_logger.info(f"Database {method_name} called successfully")
                        break
            app_logger.info("Database connection closed")
        except Exception as e:
            app_logger.error(f"Error closing database connection: {e}", exc_info=True)
        finally:
            db_instance = None
    
    # Cleanup queue producer
    if queue_producer is not None:
        try:
            # Check if the underlying queue object has a close method
            if hasattr(queue_producer, 'queue') and queue_producer.queue is not None:
                queue_obj = queue_producer.queue
                # Check for common close/stop method names
                for method_name in ['close', 'stop', 'cleanup', 'shutdown']:
                    if hasattr(queue_obj, method_name):
                        method = getattr(queue_obj, method_name)
                        if inspect.iscoroutinefunction(method):
                            await method()
                        else:
                            method()
                        app_logger.info(f"Queue {method_name} called successfully")
                        break
            app_logger.info("Queue producer closed")
        except Exception as e:
            app_logger.error(f"Error closing queue producer: {e}", exc_info=True)
        finally:
            queue_producer = None


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    description="Production-ready API for optimizing images from Google Drive to WebP format",
    version=settings.app_version,
    lifespan=lifespan
)

# Serve static assets (Tailwind CSS output, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Mount HTML web routes
from .web import router as web_router
from .public import router as public_router
from .protected import router as protected_router
app.include_router(web_router)
app.include_router(public_router)
app.include_router(protected_router)

# Add middleware (order matters!)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CORSMiddleware)
app.add_middleware(RequestIDMiddleware)
from .middleware import AuthCookieMiddleware
app.add_middleware(AuthCookieMiddleware)
app.add_middleware(RateLimitMiddleware)

# Global exception handler
@app.exception_handler(APIException)
async def api_exception_handler(request: Request, exc: APIException):
    """Handle custom API exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "error_code": exc.error_code,
            "request_id": get_request_id()
        }
    )


## Protected routes moved to api/protected.py

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions."""
    app_logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "error_code": "INTERNAL_ERROR",
            "request_id": get_request_id()
        }
    )

## Public and Job/Admin endpoints moved to routers


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
