from typing import Optional


def normalize_ui_status(status: Optional[str]) -> Optional[str]:
    """Map UI-facing status labels to backend enum values.

    running -> processing
    queued  -> pending
    otherwise returned as-is
    """
    if not status:
        return None
    if status == "running":
        return "processing"
    if status == "queued":
        return "pending"
    return status
