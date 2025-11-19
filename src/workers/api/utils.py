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
