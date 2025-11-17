from typing import Optional, Tuple, Any
from fastapi import Request, HTTPException, status
from datetime import datetime
import json
import logging
import threading

from .database import Database
from .cloudflare_queue import QueueProducer

# Internal state set by main.lifespan
_db_instance: Optional[Database] = None
_queue_producer: Optional[QueueProducer] = None
_db_lock = threading.Lock()
_services_lock = threading.Lock()
logger = logging.getLogger(__name__)


def set_db_instance(db: Database) -> None:
    global _db_instance
    with _db_lock:
        _db_instance = db


def set_queue_producer(q: QueueProducer) -> None:
    global _queue_producer
    with _db_lock:
        _queue_producer = q


def ensure_db() -> Database:
    if _db_instance is None:
        try:
            with _db_lock:
                if _db_instance is None:
                    set_db_instance(Database())
                    logger.warning("Database lazily initialized outside lifespan; consider calling set_db_instance during startup.")
        except Exception as exc:
            logger.error("Failed to initialize database: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to initialize database",
            )
    return _db_instance


def ensure_services() -> Tuple[Database, QueueProducer]:
    with _services_lock:
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
