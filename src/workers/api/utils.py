from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)


def normalize_ui_status(status: Optional[str]) -> Optional[str]:
    """Map UI-facing status labels to backend enum values.

    running -> processing
    queued  -> pending
    otherwise returned as-is
    """
    if status is None:
        return None
    if status == "running":
        return "processing"
    if status == "queued":
        return "pending"
    return status


async def enqueue_job_with_guard(
    queue,
    job_id: str,
    user_id: str,
    request,
    allow_inline_fallback: bool = False
) -> Tuple[bool, Optional[Exception], bool]:
    """
    Unified job enqueue logic with environment-aware guard.
    
    Args:
        queue: QueueProducer instance
        job_id: Job ID to enqueue
        user_id: User ID
        request: Payload dict or pydantic model
        allow_inline_fallback: If True, allows inline fallback in dev (for BackgroundTasks)
        
    Returns:
        Tuple of (enqueued: bool, exception: Optional[Exception], should_fail: bool)
        - enqueued: True if successfully enqueued
        - exception: Exception if enqueue failed, None otherwise
        - should_fail: True if caller should raise error (production + queue failed)
    """
    from .config import settings
    
    enqueued = False
    enqueue_exception: Optional[Exception] = None
    # Consider queue configured if it has a bound queue OR exposes send_generic (stub/local)
    queue_configured = (getattr(queue, "queue", None) is not None) or hasattr(queue, "send_generic")
    
    if queue_configured:
        try:
            # Support either dict payloads or objects with model_dump/__dict__
            if isinstance(request, dict):
                payload = {**request}
                if 'job_id' not in payload:
                    payload['job_id'] = job_id
                if 'user_id' not in payload:
                    payload['user_id'] = user_id
                enqueued = await queue.send_generic(payload)
            else:
                payload = request
                if hasattr(request, "model_dump"):
                    payload = request.model_dump()
                else:
                    payload = getattr(request, "__dict__", {})
                if not isinstance(payload, dict):
                    payload = {}
                if not payload:
                    logger.warning(
                        "enqueue_payload_empty",
                        extra={
                            "job_id": job_id,
                            "user_id": user_id,
                            "request_type": type(request).__name__,
                            "request_repr": repr(request)[:500],
                        },
                    )
                payload = {**payload}
                if 'job_id' not in payload:
                    payload['job_id'] = job_id
                if 'user_id' not in payload:
                    payload['user_id'] = user_id
                enqueued = await queue.send_generic(payload)
            if enqueued:
                logger.info(
                    f"Job {job_id} enqueued successfully",
                    extra={
                        "job_id": job_id,
                        "user_id": user_id,
                        "event": "job.enqueued",
                        "queue_configured": True,
                        "enqueued": True
                    }
                )
        except Exception as e:
            enqueue_exception = e
            logger.error(
                f"Failed to enqueue job {job_id}: {e}",
                exc_info=True,
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "event": "job.enqueue_failed",
                    "queue_configured": True,
                    "enqueued": False,
                    "error": str(e)
                }
            )
    else:
        logger.warning(
            f"Queue not configured for job {job_id} (docs/DEPLOYMENT.md#queue-configuration-modes)",
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "event": "job.enqueue_skipped",
                "queue_configured": False,
                "enqueued": False,
                "doc_hint": "docs/DEPLOYMENT.md#queue-configuration-modes",
            }
        )
    
    is_production = settings.environment == "production"
    
    if is_production:
        # In production, require queue and successful enqueue
        if (not queue_configured) or (not enqueued) or (enqueue_exception is not None):
            logger.error(
                f"Job {job_id} enqueue failed in production - will return error",
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "event": "job.enqueue_failed_production",
                    "environment": settings.environment,
                    "queue_configured": queue_configured,
                    "enqueued": enqueued,
                    "error": str(enqueue_exception) if enqueue_exception else None
                }
            )
            return (False, enqueue_exception, True)  # should_fail = True
    else:
        # In development, log warning if fallback will be used
        if (not queue_configured) or (not enqueued):
            logger.warning(
                f"Queue unavailable for job {job_id} - fallback {'available' if allow_inline_fallback else 'not available'} (run `python workers/consumer.py --inline` when USE_INLINE_QUEUE=true)",
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "event": "job.enqueue_fallback",
                    "environment": settings.environment,
                    "queue_configured": queue_configured,
                    "enqueued": enqueued,
                    "doc_hint": "docs/DEPLOYMENT.md#queue-configuration-modes",
                    "fallback_available": allow_inline_fallback,
                    "error": str(enqueue_exception) if enqueue_exception else None
                }
            )
    
    return (enqueued, enqueue_exception, False)  # should_fail = False
