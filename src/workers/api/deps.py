from typing import Optional, Tuple, Any
from fastapi import Request, HTTPException, status
from datetime import datetime
import json
import logging

from .database import Database
from .cloudflare_queue import QueueProducer

# Internal state set by main.lifespan
# Note: No locks needed in Cloudflare Workers - each isolate is single-threaded
_db_instance: Optional[Database] = None
_queue_producer: Optional[QueueProducer] = None
logger = logging.getLogger(__name__)


def set_db_instance(db: Database) -> None:
    global _db_instance
    _db_instance = db


def set_queue_producer(q: QueueProducer) -> None:
    global _queue_producer
    _queue_producer = q


def ensure_db() -> Database:
    """Get database instance, initializing lazily if needed.
    
    Raises HTTPException(500) if database cannot be initialized.
    This is appropriate for routes that require DB access, but middleware
    should catch this for public routes that can degrade gracefully.
    
    Note: No locks needed in Cloudflare Workers - each isolate is single-threaded.
    """
    if _db_instance is None:
        if _db_instance is None:
            try:
                set_db_instance(Database())
                logger.warning("Database lazily initialized outside lifespan; consider calling set_db_instance during startup.")
            except Exception as exc:
                logger.error("Failed to initialize database: %s", exc, exc_info=True)
                # Ensure we raise HTTPException so it can be caught by route handlers
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to initialize database",
                ) from exc
    return _db_instance


def get_queue_producer() -> Optional[QueueProducer]:
    return _queue_producer


def ensure_services() -> Tuple[Database, QueueProducer]:
    if _db_instance is None or _queue_producer is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Service not fully initialized",
        )
    return _db_instance, _queue_producer


def get_current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


def parse_job_progress(progress_str: Optional[str]) -> Any:
    try:
        data = json.loads(progress_str or "{}")
    except json.JSONDecodeError:
        data = {}
    # Return a plain dict to avoid importing pydantic models here
    return data
