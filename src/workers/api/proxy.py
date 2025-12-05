"""YouTube transcript proxy endpoint."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from .config import settings
from .deps import get_saas_user
from core.youtube_proxy import TranscriptProxyError, fetch_transcript_via_proxy
from .models import TranscriptProxyRequest, TranscriptProxyResponse

router = APIRouter()
logger = logging.getLogger(__name__)

_identity_request_log: Dict[str, list[datetime]] = {}
_identity_last_cleanup: Optional[datetime] = None
_identity_cleanup_interval = 300.0  # seconds


def _rate_limits() -> tuple[int, int]:
    minute = getattr(settings, "rate_limit_per_minute", 60)
    hour = getattr(settings, "rate_limit_per_hour", 1000)
    if minute is None:
        minute = 60
    if hour is None:
        hour = 1000
    return minute, hour


def _identity_key(user: Dict[str, Any], request: Request) -> str:
    org_id = user.get("organization_id")
    if org_id:
        return str(org_id)
    user_id = user.get("user_id")
    if user_id:
        return str(user_id)
    session_id = user.get("session_id")
    if session_id:
        return str(session_id)
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header:
        return auth_header
    host = getattr(request.client, "host", "")  # type: ignore[arg-type]
    if host:
        return host
    return "anonymous"


def _is_identity_rate_limited(identity: str) -> bool:
    minute_limit, hour_limit = _rate_limits()
    if minute_limit <= 0 and hour_limit <= 0:
        return False
    now = datetime.now(timezone.utc)

    global _identity_last_cleanup
    if (
        _identity_last_cleanup is None
        or (now - _identity_last_cleanup).total_seconds() > _identity_cleanup_interval
    ):
        for key in list(_identity_request_log.keys()):
            history = _identity_request_log[key]
            history[:] = [ts for ts in history if (now - ts).total_seconds() < 3600]
            if not history:
                del _identity_request_log[key]
        _identity_last_cleanup = now

    history = _identity_request_log.setdefault(identity, [])
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
    user: Dict[str, Any] = Depends(get_saas_user),
) -> JSONResponse | TranscriptProxyResponse:
    """Proxy YouTube transcript requests through external service using Better Auth identity."""
    identity_key = _identity_key(user, request)
    if _is_identity_rate_limited(identity_key):
        return _error_response(
            "rate_limited",
            "Too many requests for this identity. Please slow down.",
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
