"""Utility functions for the API."""
from __future__ import annotations

from typing import Optional


def redact_token(token: Optional[str], visible: int = 4) -> str:
    """
    Redact a token for safe logging/debugging.
    
    Args:
        token: The token to redact
        visible: Number of characters to show at start and end (default: 4)
    
    Returns:
        Redacted token string (e.g., "abcd...wxyz" or empty string if token is None/empty)
    """
    if not token:
        return ""
    
    if len(token) <= visible * 2:
        # Token is too short to redact meaningfully
        return "***"
    
    return f"{token[:visible]}...{token[-visible:]}"


def normalize_ui_status(status: Optional[str]) -> Optional[str]:
    """
    Normalize a UI status string to a valid JobStatusEnum value.
    
    Args:
        status: Status string from UI (e.g., "queued", "processing", "completed", etc.)
    
    Returns:
        Normalized status string matching JobStatusEnum values, or None if invalid
    """
    if not status:
        return None
    
    status_lower = status.lower().strip()
    
    # Map common UI variations to enum values
    mapping = {
        "queued": "pending",
        "running": "processing",
        "done": "completed",
        "success": "completed",
        "error": "failed",
        "canceled": "cancelled",
    }
    
    normalized = mapping.get(status_lower, status_lower)
    
    # Validate against known enum values
    valid_statuses = {"pending", "processing", "completed", "failed", "cancelled"}
    if normalized in valid_statuses:
        return normalized
    
    return None
