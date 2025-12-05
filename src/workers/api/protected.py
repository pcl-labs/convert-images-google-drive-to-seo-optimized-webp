from fastapi import APIRouter, Request, Depends, HTTPException, status, Body, Header
from fastapi.responses import RedirectResponse, PlainTextResponse
from typing import Optional, Dict, Any, Tuple, List
import uuid
import secrets
import json
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse
import re

from .config import settings
from .exceptions import DatabaseError
from .models import (
    JobStatus,
    JobProgress,
    JobListResponse,
    UserResponse,
    JobStatusEnum,
    JobType,
)
from .database import (
    create_job_extended,
    get_job,
    list_jobs,
    update_job_status,
    list_google_tokens,
    get_user_by_id,
)
from .google_oauth import (
    get_google_oauth_url,
    exchange_google_code,
    build_drive_service_for_user,
    normalize_google_integration,
    parse_google_scope_list,
)
from .constants import COOKIE_GOOGLE_OAUTH_STATE
from .app_logging import get_logger
from .exceptions import JobNotFoundError
from .simple_http import AsyncSimpleClient, HTTPStatusError, RequestError
from .deps import (
    ensure_db,
    ensure_services,
    get_saas_user,
    parse_job_progress,
)
from fastapi import Query

logger = get_logger(__name__)

router = APIRouter(
    dependencies=[Depends(get_saas_user)],
)

AGENT_SESSION_HEADER = "X-Agent-Session-Id"


def _clean_session_id(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    trimmed = value.strip()
    return trimmed or None


def get_agent_session_id(
    x_agent_session_id: Optional[str] = Header(default=None, alias=AGENT_SESSION_HEADER, convert_underscores=False),
    session_id: Optional[str] = Query(default=None, alias="session_id"),
) -> Optional[str]:
    return _clean_session_id(x_agent_session_id or session_id)


def require_agent_session_id(
    x_agent_session_id: Optional[str] = Header(default=None, alias=AGENT_SESSION_HEADER, convert_underscores=False),
    session_id: Optional[str] = Query(default=None, alias="session_id"),
) -> str:
    value = _clean_session_id(x_agent_session_id or session_id)
    if not value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Agent session_id is required")
    return value


def _validate_redirect_path(path: str, fallback: str) -> str:
    """
    Validate a redirect path to ensure it's a safe relative path.
    Rejects protocol-relative URLs (//evil.com) and absolute URLs (https://evil.com).
    Returns the validated path or the fallback if validation fails.
    """
    if not path:
        return fallback
    
    # Parse the URL to check for netloc (domain/host)
    parsed = urlparse(path)
    
    # Reject if netloc is present (absolute URL or protocol-relative URL)
    if parsed.netloc:
        return fallback
    
    # Reject if path doesn't start with a single "/" (e.g., "//evil.com")
    if not path.startswith("/") or path.startswith("//"):
        return fallback
    
    return path


def _redact_http_body_for_logging(body: Optional[str]) -> str:
    text = (body or "")
    if not text:
        return ""

    # Redact common secret-like patterns (API keys, tokens, emails, long hex/base64, auth headers, file paths)
    patterns = [
        r"sk-[A-Za-z0-9]{20,}",  # API keys
        r"(?:api|auth|session|access|refresh)_?token[=:\s]+[A-Za-z0-9._-]{10,}",
        r"Bearer\s+[A-Za-z0-9._-]{10,}",
        r"[A-Fa-f0-9]{32,}",  # long hex strings
        r"[A-Za-z0-9+/]{32,}={0,2}",  # base64-like
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",  # emails
        r"Authorization:[^\n]+",
        r"(?:/|[A-Za-z]:\\)[^\s]{10,}",  # file paths
    ]

    redacted = text
    for pattern in patterns:
        redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)

    if len(redacted) > 200:
        redacted = redacted[:200]

    return redacted


def _parse_job_progress_model(progress_str: str) -> JobProgress:
    data = parse_job_progress(progress_str) or {}
    return JobProgress(**data)


async def enqueue_job_with_guard(
    queue: Any,
    job_id: str,
    user_id: str,
    payload: Dict[str, Any],
    allow_inline_fallback: bool = False,
) -> Tuple[bool, Optional[Exception], bool]:
    """
    Enqueue a job with environment-aware error handling.
    
    Returns:
        (enqueued: bool, exception: Optional[Exception], should_fail: bool)
        - enqueued: True if successfully enqueued
        - exception: Exception if enqueue failed, None otherwise
        - should_fail: True if the caller should raise an HTTPException (production mode)
    """
    from .cloudflare_queue import QueueProducer
    
    if not isinstance(queue, QueueProducer):
        # Fallback: try to use queue directly if it's a QueueLike
        try:
            await queue.send(payload)
            return True, None, False
        except Exception as e:
            logger.error("Queue send failed", exc_info=True, extra={"job_id": job_id})
            should_fail = settings.environment == "production" and not allow_inline_fallback
            return False, e, should_fail
    
    try:
        enqueued = await queue.send_generic(payload)
        if enqueued:
            return True, None, False
        else:
            should_fail = settings.environment == "production" and not allow_inline_fallback
            return False, None, should_fail
    except Exception as e:
        logger.error("Queue send failed", exc_info=True, extra={"job_id": job_id})
        should_fail = settings.environment == "production" and not allow_inline_fallback
        return False, e, should_fail


def _summarize_google_tokens(rows: list[dict]) -> dict:
    summary: dict[str, dict] = {}
    for row in rows:
        integration = row.get("integration")
        if not integration:
            continue
        summary[integration] = {
            "expiry": row.get("expiry"),
            "scopes": parse_google_scope_list(row.get("scopes")),
            "updated_at": row.get("updated_at"),
        }
    return summary


def _parse_db_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


# Removed: _coerce_document_metadata - Documents feature removed


def _json_field(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    if value is None:
        return default
    return value


# Removed: Document serialization helpers - Documents feature removed


# Removed: All project/document helper functions - Projects/documents feature removed


# Removed: _version_detail_model - Documents feature removed


# Removed: Drive/document helper functions - Drive integration removed


def _job_status_from_row(row: dict) -> JobStatus:
    progress = _parse_job_progress_model(row.get("progress", "{}"))
    output = row.get("output")
    if isinstance(output, str) and output:
        try:
            output = json.loads(output)
        except Exception:
            output = None
    status_value = row.get("status") or JobStatusEnum.PENDING.value
    try:
        status_enum = JobStatusEnum(status_value)
    except ValueError:
        status_enum = JobStatusEnum.PENDING
    return JobStatus(
        job_id=row.get("job_id"),
        user_id=row.get("user_id"),
        status=status_enum,
        progress=progress,
        created_at=_parse_db_datetime(row.get("created_at")),
        completed_at=_parse_db_datetime(row.get("completed_at")) if row.get("completed_at") else None,
        error=row.get("error"),
        job_type=row.get("job_type"),
        document_id=row.get("document_id"),
        output=output,
        session_id=row.get("session_id"),
    )


# Removed: _pipeline_event_from_row - Pipeline events removed
# Removed: create_drive_document_for_user, _create_drive_document_for_user_removed, _ensure_drive_linked_document - Documents feature removed


# Removed: start_ingest_text_job and _derive_project_title_from_text - Text ingestion removed


# Removed: All project endpoints - Projects feature removed
# All project-related endpoints have been removed for lean YouTube proxy API
# Removed: All project endpoints (projects, blog generation, SEO analysis, etc.)


@router.get("/debug/google", tags=["Debug"])
async def debug_google_integrations(
    request: Request,
    video_id: str = Query("p12N2v2WHDA", description="YouTube video ID to test"),
    user: dict = Depends(get_saas_user),
):
    """Debug endpoint to exercise YouTube and Drive integrations from Workers.

    - Verifies that OAuth tokens can be loaded for the current user.
    - Performs lightweight YouTube and Drive API calls.
    - Logs detailed success/failure information for Cloudflare observability.
    """

    db = ensure_db()
    user_id = user.get("user_id")

    results: Dict[str, Any] = {
        "user_id": user_id,
        "video_id": video_id,
        "youtube_ok": False,
        "drive_ok": False,
    }

    # YouTube diagnostics
    try:
        logger.info(
            "debug_youtube_start",
            extra={"user_id": user_id, "video_id": video_id},
        )
        # Removed: YouTube OAuth API - use proxy endpoint instead
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="YouTube OAuth API removed. Use /api/proxy/youtube-transcript endpoint instead")
    except Exception as exc:
        results["youtube_error"] = str(exc)
        logger.error(
            "debug_youtube_error",
            exc_info=True,
            extra={"user_id": user_id, "video_id": video_id, "error": str(exc)},
        )

    # Drive diagnostics (minimal list of files from root folder)
    try:
        logger.info(
            "debug_drive_start",
            extra={"user_id": user_id},
        )
        drive_service = await build_drive_service_for_user(db, user_id)
        # Use async path in Workers runtime when available
        if hasattr(drive_service, "list_folder_files_async"):
            drive_listing = await drive_service.list_folder_files_async("root")  # type: ignore[attr-defined]
        else:
            drive_listing = await asyncio.to_thread(
                drive_service.list_folder_files,
                "root",
            )
        files = drive_listing.get("files") or []
        results["drive_ok"] = True
        results["drive_file_count"] = len(files)
        logger.info(
            "debug_drive_success",
            extra={
                "user_id": user_id,
                "file_count": len(files),
            },
        )
    except Exception as exc:
        results["drive_error"] = str(exc)
        logger.error(
            "debug_drive_error",
            exc_info=True,
            extra={"user_id": user_id, "error": str(exc)},
        )

    logger.info(
        "debug_google_integrations_complete",
        extra={"user_id": user_id, "video_id": video_id, "results": results},
    )

    return results


@router.get("/api/v1/debug/env", tags=["Debug"])
async def debug_env():
    return {
        "environment": settings.environment,
        "use_inline_queue": settings.use_inline_queue,
        "queue_bound": settings.queue is not None,
        "dlq_bound": settings.dlq is not None,
        "openai_config": {
            "api_key_set": bool(getattr(settings, "openai_api_key", None)),
            "api_base": getattr(settings, "openai_api_base", None),
            "blog_model": getattr(settings, "openai_blog_model", None),
            "blog_temperature": getattr(settings, "openai_blog_temperature", None),
            "blog_max_output_tokens": getattr(settings, "openai_blog_max_output_tokens", None),
        },
        "ai_gateway_config": {
            "cloudflare_account_id": getattr(settings, "cloudflare_account_id", None),
            "token_set": bool(getattr(settings, "cf_ai_gateway_token", None)),
            "openai_api_base": getattr(settings, "openai_api_base", None),
        },
    }


# Removed: GitHub OAuth status endpoint - GitHub OAuth removed


@router.get("/auth/google/start", tags=["Authentication"])
async def google_auth_start(
    request: Request,
    integration: str = Query("drive", description="Google integration to connect (drive, youtube, gmail)"),
    redirect: Optional[str] = Query(None, description="Optional path to redirect after linking"),
    user: dict = Depends(get_saas_user),
):
    """Start Google OAuth flow for linking an integration.
    
    Stores OAuth state in user's session instead of cookies for better cross-site redirect reliability.
    """
    from .database import touch_user_session
    import json
    
    # Check OAuth configuration early to provide better error message
    if not settings.google_client_id or not settings.google_client_secret:
        # Redirect back to integrations page (for browser requests)
        integration_key = normalize_google_integration(integration)
        redirect_path = redirect or f"/dashboard/integrations/{integration_key}"
        redirect_path = _validate_redirect_path(redirect_path, "/dashboard/integrations")
        return RedirectResponse(url=redirect_path, status_code=status.HTTP_302_FOUND)
    
    try:
        integration_key = normalize_google_integration(integration)
        if settings.base_url:
            redirect_uri = f"{settings.base_url.rstrip('/')}/auth/google/callback"
        else:
            redirect_uri = str(request.url.replace(path="/auth/google/callback", query=""))
        state = secrets.token_urlsafe(16)
        auth_url = get_google_oauth_url(state, redirect_uri, integration=integration_key)

        # Store OAuth state in user's session instead of cookies
        # This is more reliable for cross-site redirects
        db = ensure_db()
        user_id = user["user_id"]
        session = getattr(request.state, "session", None)
        session_id = getattr(request.state, "session_id", None)
        
        logger.debug(
            "Google integration OAuth start: session_present=%s, session_id=%s, user_id=%s",
            session is not None,
            session_id,
            user_id,
        )
        
        # If no session exists, create one for this authenticated user
        if not session_id:
            from .database import create_user_session, create_user, get_user_by_id
            from datetime import timedelta
            # Ensure user exists in database (required for foreign key constraint in user_sessions)
            # Check if user exists first to avoid UNIQUE constraint violations on github_id/google_id
            existing_user = await get_user_by_id(db, user_id)
            if not existing_user:
                # Only create if user doesn't exist
                # create_user handles UNIQUE constraint violations gracefully by returning existing user
                try:
                    created_user = await create_user(
                        db,
                        user_id,
                        github_id=user.get("github_id"),
                        google_id=user.get("google_id"),
                        email=user.get("email"),
                    )
                    # Use the returned user (might be different if UNIQUE constraint returned existing user)
                    existing_user = created_user
                except Exception as create_error:
                    # If create fails, try to get the existing user - they might have been created by another request
                    logger.warning(f"create_user failed, checking if user exists: {create_error}")
                    existing_user = await get_user_by_id(db, user_id)
                    if not existing_user:
                        # If user still doesn't exist, we can't create a session - this is a critical error
                        logger.error(f"Cannot create session: user {user_id} does not exist and could not be created: {create_error}")
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to create user account. Please try again."
                        ) from create_error
            # Use the existing user's user_id (might be different if UNIQUE constraint returned different user)
            actual_user_id = existing_user.get("user_id") or user_id
            # Verify user exists before creating session
            if not actual_user_id:
                logger.error(f"Cannot create session: user_id is None for user {user_id}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="User account error. Please try again."
                )
            session_id = secrets.token_urlsafe(32)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
            await create_user_session(
                db,
                session_id,
                actual_user_id,
                expires_at,
                ip_address=(request.client.host if request.client else None),
                user_agent=request.headers.get("user-agent"),
                extra={"oauth_state": state, "google_redirect_uri": redirect_uri, "google_integration": integration_key},
            )
            redirect_path = redirect or f"/dashboard/integrations/{integration_key}"
            redirect_path = _validate_redirect_path(redirect_path, f"/dashboard/integrations/{integration_key}")
            # Update with redirect path
            from .database import touch_user_session
            await touch_user_session(
                db,
                session_id,
                extra={"oauth_state": state, "google_redirect_uri": redirect_uri, "google_integration": integration_key, "google_redirect_next": redirect_path},
            )
            logger.info("Google integration OAuth: Created session %s and stored state for user %s", session_id, user_id)
            
            # Set session cookie in response
            is_secure = settings.environment == "production" or request.url.scheme == "https"
            response = RedirectResponse(url=auth_url)
            response.set_cookie(
                key=settings.session_cookie_name,
                value=session_id,
                httponly=True,
                secure=is_secure,
                samesite="lax",
                max_age=int(settings.session_ttl_hours * 3600),
                path="/",
            )
            return response
        else:
            # Update session extra with OAuth state
            current_extra = json.loads(session.get("extra", "{}")) if isinstance(session.get("extra"), str) else (session.get("extra") or {})
            current_extra["oauth_state"] = state
            current_extra["google_redirect_uri"] = redirect_uri
            current_extra["google_integration"] = integration_key
            redirect_path = redirect or f"/dashboard/integrations/{integration_key}"
            redirect_path = _validate_redirect_path(redirect_path, f"/dashboard/integrations/{integration_key}")
            current_extra["google_redirect_next"] = redirect_path
            
            await touch_user_session(db, session_id, extra=current_extra)
            logger.info("Google integration OAuth: Stored state in session %s for user %s", session_id, user_id)
        
        # If we stored in session, just redirect
        response = RedirectResponse(url=auth_url)
        return response
    except ValueError as e:
        # ValueError from get_google_oauth_url when OAuth is not configured
        if "Google OAuth not configured" in str(e):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google OAuth is not configured. Please contact support."
            ) from e
        raise
    except DatabaseError as e:
        # Database errors (including UNIQUE constraint violations)
        logger.error(f"Google auth initiation failed (database error): {e}", exc_info=True)
        # Check if it's a UNIQUE constraint violation
        from .database import _is_unique_constraint_violation
        if _is_unique_constraint_violation(e):
            # Try to redirect with error message instead of 500
            integration_key = normalize_google_integration(integration)
            redirect_path = redirect or f"/dashboard/integrations/{integration_key}"
            redirect_path = _validate_redirect_path(redirect_path, "/dashboard/integrations")
            return RedirectResponse(url=redirect_path, status_code=status.HTTP_302_FOUND)
        # Re-raise other database errors
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error occurred. Please try again later."
        ) from e
    except Exception as e:
        logger.error(f"Google auth initiation failed: {e}", exc_info=True)
        # Don't assume it's an OAuth configuration issue - show the actual error
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred: {str(e)}"
        ) from e


@router.get("/auth/google/callback", tags=["Authentication"])
async def google_auth_callback(code: str, state: str, request: Request, user: dict = Depends(get_saas_user)):
    """Handle Google OAuth callback for integration linking.
    
    Retrieves OAuth state from user's session (preferred) or cookies (fallback).
    """
    from .database import touch_user_session
    import json
    
    db = ensure_db()

    # Try to get state from session first (preferred method)
    stored_state = None
    redirect_uri = None
    integration_key = None
    next_path = None
    
    session = getattr(request.state, "session", None)
    session_id = getattr(request.state, "session_id", None)
    
    # If session isn't loaded by middleware, try to load it manually from cookie
    # This can happen on cross-site redirects where middleware might not have loaded it
    # or when session is not in cache (middleware skips DB lookup to avoid ASGI errors)
    if not session or not session_id:
        session_cookie = request.cookies.get(settings.session_cookie_name)
        if session_cookie:
            from .database import get_user_session
            try:
                loaded_session = await get_user_session(db, session_cookie)
                if loaded_session:
                    session = loaded_session
                    session_id = session_cookie
                    logger.debug("Google integration callback: Manually loaded session %s from cookie", session_id)
                else:
                    logger.debug("Google integration callback: Session cookie %s not found in database", session_cookie)
            except Exception as exc:
                logger.warning("Google integration callback: Failed to manually load session: %s", exc, exc_info=True)
    
    logger.debug(
        "Google integration callback: session_present=%s, session_id=%s, cookies=%s",
        session is not None,
        session_id,
        list(request.cookies.keys()),
    )
    
    if session and session_id:
        session_extra = session.get("extra")
        if session_extra:
            if isinstance(session_extra, str):
                try:
                    session_extra = json.loads(session_extra)
                except Exception:
                    session_extra = {}
            else:
                session_extra = session_extra or {}
            
            stored_state = session_extra.get("oauth_state")
            redirect_uri = session_extra.get("google_redirect_uri")
            integration_key = session_extra.get("google_integration")
            next_path = session_extra.get("google_redirect_next")
            
            logger.debug(
                "Google integration callback: Found in session - state_present=%s, integration=%s",
                stored_state is not None,
                integration_key,
            )
            
            # Clean up OAuth state from session after retrieving
            if stored_state:
                session_extra.pop("oauth_state", None)
                session_extra.pop("google_redirect_uri", None)
                session_extra.pop("google_integration", None)
                session_extra.pop("google_redirect_next", None)
                await touch_user_session(db, session_id, extra=session_extra)
                logger.info("Google integration OAuth: Retrieved state from session %s", session_id)
    
    # Fallback to cookies if not found in session
    if not stored_state:
        stored_state = request.cookies.get(COOKIE_GOOGLE_OAUTH_STATE)
        if not redirect_uri:
            redirect_uri = request.cookies.get("google_redirect_uri")
        if not integration_key:
            integration_cookie = request.cookies.get("google_integration")
            if integration_cookie:
                try:
                    integration_key = normalize_google_integration(integration_cookie)
                except Exception:
                    pass
        if not next_path:
            next_path = request.cookies.get("google_redirect_next")
        
        logger.debug(
            "Google integration callback: Fallback to cookies - state_present=%s, integration=%s",
            stored_state is not None,
            integration_key,
        )

    # Verify state
    if not stored_state:
        logger.warning(
            "Google OAuth state verification failed - no stored state found. session_id=%s, cookies=%s",
            session_id,
            list(request.cookies.keys()),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid state parameter - possible CSRF attack")
    
    if not secrets.compare_digest(stored_state, state):
        logger.warning(
            "Google OAuth state verification failed - state mismatch. stored_length=%d, received_length=%d",
            len(stored_state) if stored_state else 0,
            len(state) if state else 0,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid state parameter - possible CSRF attack")

    # Validate integration
    if not redirect_uri:
        redirect_uri = str(request.url.replace(query=""))
    if not integration_key:
        try:
            integration_key = normalize_google_integration(None)  # Will use default
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing or invalid Google integration selection")

    try:
        await exchange_google_code(db, user["user_id"], code, redirect_uri, integration=integration_key)
        is_secure = settings.environment == "production" or request.url.scheme == "https"
        if not next_path:
            next_path = f"/dashboard/integrations/{integration_key}"
        next_path = _validate_redirect_path(next_path, f"/dashboard/integrations/{integration_key}")
        response = RedirectResponse(url=next_path, status_code=status.HTTP_302_FOUND)
        
        # Clean up cookies (in case fallback was used)
        response.delete_cookie(key=COOKIE_GOOGLE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
        response.delete_cookie(key="google_redirect_uri", path="/", samesite="lax", httponly=True, secure=is_secure)
        response.delete_cookie(key="google_integration", path="/", samesite="lax", httponly=True, secure=is_secure)
        response.delete_cookie(key="google_redirect_next", path="/", samesite="lax", httponly=True, secure=is_secure)
        return response
    except Exception as e:
        logger.error(f"Google callback failed: {e}", exc_info=True)
        error_detail = str(e) if settings.debug else "Google authentication failed"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error_detail) from None


@router.get("/auth/google/status", tags=["Authentication"])
async def google_link_status(user: dict = Depends(get_saas_user)):
    db = ensure_db()
    rows = await list_google_tokens(db, user["user_id"])  # type: ignore
    summary = _summarize_google_tokens(rows)
    return {
        "linked": bool(summary),
        "integrations": summary,
    }


@router.get("/auth/providers/status", tags=["Authentication"])
async def providers_status(user: dict = Depends(get_saas_user)):
    """Get status of linked OAuth providers (Google only)."""
    db = ensure_db()
    rows = await list_google_tokens(db, user["user_id"])  # type: ignore
    summary = _summarize_google_tokens(rows)
    return {
        "google_linked": bool(summary),
        "google_integrations": summary,
    }


@router.get("/auth/me", response_model=UserResponse, tags=["Authentication"])
async def get_current_user_info(request: Request):
    """Get current authenticated user information.
    
    This endpoint uses get_saas_user dependency semantics internally.
    Architectural change: Uses Request parameter directly for Cloudflare Workers compatibility.
    """
    user = await get_saas_user(request)
    
    # Fetch created_at from database if not in user dict
    created_at = None
    if not user.get("created_at"):
        try:
            db = ensure_db()
            db_user = await get_user_by_id(db, user["user_id"])
            if db_user:
                created_at_raw = db_user.get("created_at")
                if created_at_raw:
                    if isinstance(created_at_raw, datetime):
                        created_at = created_at_raw
                    else:
                        # Parse string timestamp
                        try:
                            created_at = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
                            if created_at.tzinfo is None:
                                created_at = created_at.replace(tzinfo=timezone.utc)
                        except (ValueError, AttributeError):
                            pass
        except Exception:
            # If DB fetch fails, use current time as fallback
            pass
    
    # Use current time as fallback if created_at not available
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    
    return UserResponse(
        user_id=user.get("user_id", "unknown"),
        github_id=user.get("github_id"),
        email=user.get("email"),
        created_at=created_at,
    )


# Removed: All document endpoints - Documents feature removed
# Removed: link_drive_document_endpoint, start_optimize_job, optimize, generate_blog endpoints - Documents feature removed


@router.get("/api/v1/jobs/{job_id}", response_model=JobStatus, tags=["Jobs"])
async def get_job_status(job_id: str, user: dict = Depends(get_saas_user)):
    db = ensure_db()
    job = await get_job(db, job_id, user["user_id"])
    if not job:
        raise JobNotFoundError(job_id)
    return _job_status_from_row(job)


@router.get("/api/v1/jobs", response_model=JobListResponse, tags=["Jobs"])
async def list_user_jobs(
    page: int = 1,
    page_size: int = 20,
    status_filter: Optional[JobStatusEnum] = None,
    user: dict = Depends(get_saas_user),
    agent_session_id: Optional[str] = Depends(get_agent_session_id),
):
    db = ensure_db()
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20
    jobs_list, total = await list_jobs(
        db,
        user["user_id"],
        page=page,
        page_size=page_size,
        status=status_filter.value if status_filter else None,
        session_id=agent_session_id,
    )
    job_statuses = [_job_status_from_row(job) for job in jobs_list]
    return JobListResponse(jobs=job_statuses, total=total, page=page, page_size=page_size, has_more=(page * page_size) < total)


# Removed: Sessions/events and text ingestion endpoints - Not needed for YouTube proxy API


@router.delete("/api/v1/jobs/{job_id}", tags=["Jobs"])
async def cancel_job(job_id: str, user: dict = Depends(get_saas_user)):
    db = ensure_db()
    job = await get_job(db, job_id, user["user_id"])
    if not job:
        raise JobNotFoundError(job_id)
    current_status = JobStatusEnum(job["status"])
    if current_status in [JobStatusEnum.COMPLETED, JobStatusEnum.FAILED, JobStatusEnum.CANCELLED]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot cancel job with status: {current_status.value}")
    await update_job_status(db, job_id, "cancelled")
    logger.info(f"Cancelled job {job_id} for user {user['user_id']}")
    return {"ok": True, "job_id": job_id}


# Removed: Stats and usage endpoints - Not needed for YouTube proxy API
