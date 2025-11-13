from typing import Optional, Tuple, Any
from fastapi import Request, HTTPException, status
from datetime import datetime
import json

from .database import Database
from .cloudflare_queue import QueueProducer

# Internal state set by main.lifespan
_db_instance: Optional[Database] = None
_queue_producer: Optional[QueueProducer] = None


def set_db_instance(db: Database) -> None:
    global _db_instance
    _db_instance = db


def set_queue_producer(q: QueueProducer) -> None:
    global _queue_producer
    _queue_producer = q


def ensure_db() -> Database:
    if _db_instance is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database not initialized",
        )
    return _db_instance


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
