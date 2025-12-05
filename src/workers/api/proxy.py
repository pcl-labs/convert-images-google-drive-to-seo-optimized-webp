"""YouTube transcript proxy endpoint."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from .auth import authenticate_api_key
from .config import settings
from .deps import ensure_db
from .models import TranscriptProxyRequest, TranscriptProxyResponse
from core.youtube_proxy import TranscriptProxyError, fetch_transcript_via_proxy

router = APIRouter()
logger = logging.getLogger(__name__)

# Rate limiting state - no lock needed since Workers are single-threaded per isolate
_api_key_request_log: Dict[str, list[datetime]] = {}
_api_key_last_cleanup: Optional[datetime] = None
_api_key_cleanup_interval = 300.0  # seconds


def _extract_api_key(auth_header: Optional[str], body_api_key: Optional[str]) -> Optional[str]:
    """Extract API key from Authorization header or request body."""
    if auth_header:
        prefix = "Bearer "
        if auth_header.startswith(prefix):
            token = auth_header[len(prefix) :].strip()
            if token:
                return token
    if body_api_key:
        return body_api_key.strip() or None
    return None


def _rate_limits() -> tuple[int, int]:
    minute = getattr(settings, "rate_limit_per_minute", 60)
    hour = getattr(settings, "rate_limit_per_hour", 1000)
    if minute is None:
        minute = 60
    if hour is None:
        hour = 1000
    return minute, hour


async def _is_api_key_rate_limited(api_key: str) -> bool:
    """Track API key usage with simple in-memory rate limiting.
    
    Uses datetime instead of time.monotonic() and no locks since Workers are single-threaded per isolate.
    """
    minute_limit, hour_limit = _rate_limits()
    if minute_limit <= 0 and hour_limit <= 0:
        return False
    
    # Use datetime instead of time.monotonic() per Cloudflare Workers gotchas
    now = datetime.now(timezone.utc)
    
    # No lock needed - Workers are single-threaded per isolate
    global _api_key_last_cleanup
    if _api_key_last_cleanup is None or (now - _api_key_last_cleanup).total_seconds() > _api_key_cleanup_interval:
        # Clean up old entries
        for key in list(_api_key_request_log.keys()):
            history = _api_key_request_log[key]
            history[:] = [ts for ts in history if (now - ts).total_seconds() < 3600]
            if not history:
                del _api_key_request_log[key]
        _api_key_last_cleanup = now
    
    # Check rate limits for this API key
    history = _api_key_request_log.setdefault(api_key, [])
    history[:] = [ts for ts in history if (now - ts).total_seconds() < 3600]
    requests_last_hour = len(history)
    requests_last_minute = len([ts for ts in history if (now - ts).total_seconds() < 60])
    
    if (minute_limit > 0 and requests_last_minute >= minute_limit) or (
        hour_limit > 0 and requests_last_hour >= hour_limit
    ):
        return True
    
    history.append(now)
    return False


def _error_response(
    code: str,
    message: str,
    *,
    details: Optional[Any] = None,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> JSONResponse:
    """Build standardized error response."""
    payload = TranscriptProxyResponse(
        success=False,
        error={
            "code": code,
            "message": message,
            **({"details": details} if details is not None else {}),
        },
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(exclude_none=True))


@router.post("/api/proxy/youtube-transcript", response_model=TranscriptProxyResponse, tags=["Proxy"])
async def proxy_youtube_transcript(
    request_body: TranscriptProxyRequest,
    request: Request,
) -> JSONResponse | TranscriptProxyResponse:
    """Proxy YouTube transcript requests through external service using API key auth."""
    api_key = _extract_api_key(request.headers.get("Authorization"), request_body.api_key)
    if not api_key:
        return _error_response(
            "missing_api_key",
            "API key is required. Provide it via Authorization header or api_key field.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    
    db = ensure_db()
    api_user = await authenticate_api_key(db, api_key)
    if not api_user:
        return _error_response(
            "invalid_api_key",
            "Provided API key is invalid.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    
    # Attach user_id for downstream middleware visibility
    request.state.user_id = api_user.get("user_id")
    
    if await _is_api_key_rate_limited(api_key):
        return _error_response(
            "rate_limited",
            "Too many requests for this API key. Please slow down.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    
    try:
        result = await fetch_transcript_via_proxy(request_body.video_id)
        
        transcript_data = result.get("transcript", {})
        metadata_data = result.get("metadata", {})
        
        return TranscriptProxyResponse(
            success=True,
            transcript={
                "text": transcript_data.get("text", ""),
                "format": transcript_data.get("format", "json3"),
                "language": transcript_data.get("language"),
                "track_kind": transcript_data.get("trackKind"),
            },
            metadata={
                "client_version": metadata_data.get("clientVersion"),
                "method": metadata_data.get("method", "innertube"),
                "video_id": metadata_data.get("videoId", request_body.video_id),
            },
        )
        
    except TranscriptProxyError as exc:
        return _error_response(
            exc.code,
            exc.message,
            details=exc.details,
            status_code=exc.status_code,
        )
        
    except ValueError as exc:
        return _error_response(
            "invalid_request",
            str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
        
    except Exception as exc:
        logger.exception("youtube_transcript_proxy_unexpected_error", extra={"video_id": request_body.video_id})
        return _error_response(
            "internal_error",
            "An unexpected error occurred while fetching transcript.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
