"""Flash message utilities for server-driven toast notifications."""

import json
import logging
from typing import Optional
from fastapi import Request

from .deps import ensure_db
from .database import touch_user_session

logger = logging.getLogger(__name__)


async def add_flash(
    request: Request,
    message: str,
    category: str = "info",
    href: Optional[str] = None,
) -> None:
    """Add a flash message to the session for display on the next page load.
    
    Flash messages are stored in session.extra and displayed as toasts.
    They are automatically cleared after being read once.
    
    Args:
        request: FastAPI request object
        message: Message text to display
        category: Message category - 'success', 'error', 'info', or 'warning'
        href: Optional link URL to include in the toast
    """
    session = getattr(request.state, "session", None)
    session_id = getattr(request.state, "session_id", None)
    
    if not session or not session_id:
        logger.debug("Cannot add flash message: no session available")
        return
    
    # Prepare flash message
    flash_msg = {
        "type": category,
        "text": message,
        "href": href,
    }
    
    # Get or create extra dict
    extra = session.get("extra")
    if extra:
        try:
            if isinstance(extra, str):
                extra_dict = json.loads(extra)
            else:
                extra_dict = extra
        except (json.JSONDecodeError, TypeError):
            extra_dict = {}
    else:
        extra_dict = {}
    
    # Add to flash queue
    flash_queue = extra_dict.get("flash_messages", [])
    if not isinstance(flash_queue, list):
        flash_queue = []
    flash_queue.append(flash_msg)
    extra_dict["flash_messages"] = flash_queue
    
    # Update session
    try:
        db = ensure_db()
        await touch_user_session(db, session_id, extra=extra_dict)
        # Update in-memory session
        session["extra"] = json.dumps(extra_dict) if isinstance(extra_dict, dict) else extra_dict
    except Exception as exc:
        logger.warning("Failed to persist flash message to session: %s", exc)

