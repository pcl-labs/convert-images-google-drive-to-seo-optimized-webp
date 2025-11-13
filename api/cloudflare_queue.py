"""
Cloudflare Queues integration for background job processing.
"""

import json
import logging
import datetime
from typing import Dict, Any, Optional, Protocol

from .config import settings
from .models import OptimizeRequest

logger = logging.getLogger(__name__)


class QueueLike(Protocol):
    async def send(self, message: Dict[str, Any]) -> Any: ...


class QueueProducer:
    """Producer for sending jobs to Cloudflare Queues."""
    
    def __init__(self, queue: Optional["QueueLike"] = None, dlq: Optional["QueueLike"] = None):
        """Initialize queue producer."""
        self.queue: Optional[QueueLike] = queue or settings.queue
        self.dlq: Optional[QueueLike] = dlq or settings.dlq
    
    async def send_job(self, job_id: str, user_id: str, request: OptimizeRequest) -> bool:
        """Send a job to the queue."""
        if not self.queue:
            logger.warning("Queue not configured, job will not be processed")
            return False
        
        try:
            message = {
                "job_id": job_id,
                "user_id": user_id,
                "drive_folder": request.drive_folder,
                "extensions": request.extensions,
                "overwrite": request.overwrite,
                "skip_existing": request.skip_existing,
                "cleanup_originals": request.cleanup_originals,
                "max_retries": request.max_retries,
            }
            
            # Send to queue
            await self.queue.send(message)
            logger.info(f"Sent job {job_id} to queue")
            return True
        except Exception as e:
            logger.error(f"Failed to send job {job_id} to queue: {e}", exc_info=True)
            return False
    
    async def send_to_dlq(self, job_id: str, error: str, original_message: Dict[str, Any]) -> bool:
        """Send a failed job to the dead letter queue."""
        if not self.dlq:
            logger.warning("Dead letter queue not configured, job will not be sent to DLQ")
            return False
        
        try:
            dlq_message = {
                "job_id": job_id,
                "error": error,
                "original_message": original_message,
                "failed_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
            
            # Send to dead letter queue
            await self.dlq.send(dlq_message)
            logger.info(f"Job {job_id} sent to DLQ: {error}")
            return True
        except Exception as e:
            logger.error(f"Failed to send job {job_id} to DLQ: {e}", exc_info=True)
            return False


class QueueConsumer:
    """Consumer for processing jobs from Cloudflare Queues."""
    
    def __init__(self, queue: Optional["QueueLike"] = None):
        """Initialize queue consumer."""
        self.queue: Optional[QueueLike] = queue or settings.queue
    
    async def process_message(self, message: Dict[str, Any]) -> bool:
        """Process a message from the queue."""
        try:
            job_id = message.get("job_id")
            user_id = message.get("user_id")
            
            if not job_id or not user_id:
                logger.error("Invalid message: missing job_id or user_id")
                return False
            
            logger.info(f"Processing job {job_id} for user {user_id}")
            
            # The actual processing will be done in worker_consumer.py
            # This is just the interface
            
            return True
        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            return False
