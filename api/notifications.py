import uuid
from typing import Optional
from .database import Database, create_notification

async def notify_job(db: Database, user_id: str, job_id: str, level: str, text: Optional[str] = None) -> None:
    """Create a notification for a job using a UUID id.
    Level: 'success' | 'error' | 'info'
    """
    allowed = {"success", "error", "info"}
    if level not in allowed:
        raise ValueError(f"Invalid level '{level}'. Allowed: {sorted(allowed)}")
    notif_id = str(uuid.uuid4())
    await create_notification(
        db,
        notif_id=notif_id,
        user_id=user_id,
        level=level,
        text=text or f"Job {job_id}",
        title=None,
        context={"job_id": job_id},
        event_id=None,
    )
