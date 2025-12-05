"""YouTube transcript proxy endpoint."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Request, status, HTTPException
from fastapi.responses import JSONResponse

from .config import settings
from .deps import get_saas_user
from .better_auth import (
    YouTubeIntegration,
    fetch_youtube_integration,
    refresh_youtube_access_token,
)
from core.youtube_proxy import (
    TranscriptProxyError,
    fetch_transcript_via_proxy,
    fetch_transcript_via_youtube_api,
)
from .models import TranscriptProxyRequest, TranscriptProxyResponse

router = APIRouter()
logger = logging.getLogger(__name__)

_identity_request_log: Dict[str, list[datetime]] = {}
_identity_last_cleanup: Optional[datetime] = None
_identity_cleanup_interval = 300.0  # seconds

YOUTUBE_LINK_HINT = (
    "Link your YouTube account in Settings → Integrations to unlock higher-accuracy transcripts."
)


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


def _is_expired(expires_at: Optional[datetime]) -> bool:
    if not expires_at:
        return False
    return expires_at <= datetime.now(timezone.utc)


async def _try_youtube_api_primary(
    request: Request, video_id: str
) -> Tuple[Optional[TranscriptProxyResponse], Optional[str]]:
    integration, forbidden = await fetch_youtube_integration(request)
    if forbidden:
        logger.info("youtube_api_access_forbidden")
        return None, YOUTUBE_LINK_HINT
    if not integration:
        return None, YOUTUBE_LINK_HINT

    token = integration.access_token
    if _is_expired(integration.expires_at):
        if not integration.refresh_token:
            logger.warning(
                "youtube_api_token_expired_no_refresh",
                extra={"integration_id": integration.integration_id},
            )
            return None, YOUTUBE_LINK_HINT
        try:
            refreshed = await refresh_youtube_access_token(integration.refresh_token)
        except HTTPException as exc:
            logger.warning(
                "youtube_api_token_refresh_failed",
                extra={"status": exc.status_code if isinstance(exc, HTTPException) else None},
            )
            return None, YOUTUBE_LINK_HINT
        token = refreshed.get("access_token")

    if not token:
        logger.warning(
            "youtube_api_missing_access_token",
            extra={"integration_id": integration.integration_id},
        )
        return None, YOUTUBE_LINK_HINT

    try:
        result = await fetch_transcript_via_youtube_api(video_id, token)
    except TranscriptProxyError as fallback_exc:
        logger.warning(
            "youtube_api_primary_failed",
            extra={"video_id": video_id, "error_code": fallback_exc.code},
        )
        hint = (
            YOUTUBE_LINK_HINT
            if fallback_exc.code in {"permission_denied", "auth_failed", "youtube_not_owner"}
            else None
        )
        return None, hint

    logger.info("youtube_api_primary_success", extra={"video_id": video_id})
    return _response_from_result(result, video_id), None


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


def _response_from_result(
    result: Dict[str, Any], fallback_video_id: str, *, hint: Optional[str] = None
) -> TranscriptProxyResponse:
    transcript_data = result.get("transcript", {})
    metadata_data = result.get("metadata", {})
    metadata = {
        "client_version": metadata_data.get("clientVersion"),
        "method": metadata_data.get("method"),
        "video_id": metadata_data.get("videoId", fallback_video_id),
        "caption_id": metadata_data.get("captionId"),
    }
    if hint:
        metadata["accountLinkHint"] = hint
    return TranscriptProxyResponse(
        success=True,
        transcript={
            "text": transcript_data.get("text", ""),
            "format": transcript_data.get("format", "text"),
            "language": transcript_data.get("language"),
            "track_kind": transcript_data.get("trackKind"),
        },
        metadata=metadata,
    )


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
    
    youtube_hint: Optional[str] = None
    youtube_response, youtube_hint = await _try_youtube_api_primary(request, request_body.video_id)
    if youtube_response:
        if youtube_hint:
            metadata = dict(youtube_response.metadata or {})
            metadata["accountLinkHint"] = youtube_hint
            youtube_response = youtube_response.model_copy(update={"metadata": metadata})
        return youtube_response

    try:
        result = await fetch_transcript_via_proxy(request_body.video_id)
        return _response_from_result(result, request_body.video_id, hint=youtube_hint)

    except TranscriptProxyError as exc:
        details = exc.details or {}
        if youtube_hint:
            merged = dict(details)
            merged.setdefault("accountLinkHint", youtube_hint)
            details = merged
        return _error_response(
            exc.code,
            exc.message,
            details=details,
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
