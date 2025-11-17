import uuid
from typing import Optional, Dict, Any

from .database import Database, create_notification

_ALLOWED_LEVELS = {"success", "error", "info"}


async def notify_activity(
    db: Database,
    user_id: str,
    level: str,
    text: str,
    *,
    title: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    event_id: Optional[str] = None,
) -> None:
    """Emit a generic activity notification."""
    if level not in _ALLOWED_LEVELS:
        raise ValueError(f"Invalid level '{level}'. Allowed: {sorted(_ALLOWED_LEVELS)}")
    notif_id = str(uuid.uuid4())
    await create_notification(
        db,
        notif_id=notif_id,
        user_id=user_id,
        level=level,
        text=text,
        title=title,
        context=context or {},
        event_id=event_id,
    )


async def notify_job(
    db: Database,
    user_id: str,
    job_id: str,
    level: str,
    text: Optional[str] = None,
) -> None:
    """Specialized helper for job-centric notifications."""
    await notify_activity(
        db,
        user_id,
        level,
        text or f"Job {job_id}",
        context={"job_id": job_id},
    )
