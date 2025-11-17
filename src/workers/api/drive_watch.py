from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from .app_logging import get_logger
from .config import settings
from .database import (
    Database,
    delete_drive_watch,
    get_drive_watch_by_document,
    get_drive_watch_by_channel,
    list_drive_watches_expiring,
    update_drive_watch_fields,
    upsert_drive_watch,
)
from .google_oauth import build_drive_service_for_user
from src.workers.core.google_async import execute_google_request

logger = get_logger(__name__)


def build_channel_token(channel_id: str, user_id: str, document_id: str) -> str:
    if not settings.drive_webhook_secret:
        raise RuntimeError("DRIVE_WEBHOOK_SECRET is required to register Drive webhooks")
    payload = f"{user_id}:{document_id}:{channel_id}".encode("utf-8")
    secret = settings.drive_webhook_secret.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _parse_expiration(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        # Google returns milliseconds since epoch
        ts_ms = int(raw)
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        logger.warning("drive_watch_expiration_parse_failed", extra={"raw": raw})
        return None


def _needs_renewal(watch: Dict[str, str]) -> bool:
    expires_at = watch.get("expires_at")
    if not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    buffer_minutes = max(int(getattr(settings, "drive_watch_renewal_window_minutes", 60) or 60), 1)
    renew_after = datetime.now(timezone.utc) + timedelta(minutes=buffer_minutes)
    return expiry <= renew_after


async def ensure_drive_watch(
    db: Database,
    *,
    user_id: str,
    document_id: str,
    drive_file_id: str,
    force: bool = False,
) -> Optional[Dict[str, str]]:
    if not settings.drive_webhook_url:
        logger.debug("drive_webhook_url not configured; skipping Drive watch registration")
        return None
    existing = await get_drive_watch_by_document(db, document_id)
    if existing and not force and not _needs_renewal(existing):
        return existing
    try:
        drive_service = await build_drive_service_for_user(db, user_id)  # type: ignore
    except Exception as exc:
        logger.warning("drive_watch_drive_auth_failed", extra={"user_id": user_id, "error": str(exc)})
        return None
    channel_id = str(uuid.uuid4())
    try:
        token = build_channel_token(channel_id, user_id, document_id)
    except RuntimeError as exc:
        logger.error(
            "drive_watch_token_missing_secret",
            exc_info=True,
            extra={"user_id": user_id, "document_id": document_id, "channel_id": channel_id},
        )
        return None
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": settings.drive_webhook_url,
        "token": token,
        "payload": True,
    }
    try:
        response = await execute_google_request(
            drive_service.files().watch(
                fileId=drive_file_id,
                body=body,
                supportsAllDrives=True,
            )
        )
    except Exception as exc:
        logger.error(
            "drive_watch_register_failed",
            exc_info=True,
            extra={"document_id": document_id, "drive_file_id": drive_file_id, "error": str(exc)},
        )
        return None
    expires_at = _parse_expiration((response or {}).get("expiration"))
    watch_id = str(uuid.uuid4())
    stored = await upsert_drive_watch(
        db,
        watch_id=watch_id,
        user_id=user_id,
        document_id=document_id,
        drive_file_id=drive_file_id,
        channel_id=channel_id,
        resource_id=(response or {}).get("resourceId"),
        resource_uri=(response or {}).get("resourceUri"),
        expires_at=expires_at,
        state="active",
    )
    logger.info(
        "drive_watch_registered",
        extra={
            "document_id": document_id,
            "channel_id": channel_id,
            "expires_at": expires_at,
        },
    )
    return stored


async def mark_watch_stopped(db: Database, channel_id: str) -> None:
    watch = await get_drive_watch_by_channel(db, channel_id)
    if not watch:
        return
    await delete_drive_watch(db, user_id=watch.get("user_id"), channel_id=channel_id)
    logger.info(
        "drive_watch_stopped",
        extra={"document_id": watch.get("document_id"), "channel_id": channel_id},
    )


async def update_watch_expiration(
    db: Database,
    *,
    watch_id: str,
    user_id: str,
    expiration: Optional[str],
) -> None:
    if expiration is None:
        return
    await update_drive_watch_fields(db, user_id=user_id, watch_id=watch_id, expires_at=expiration)


async def watches_due_for_renewal(db: Database, within_minutes: int, user_id: Optional[str] = None) -> list[Dict[str, str]]:
    seconds = max(within_minutes * 60, 60)
    return await list_drive_watches_expiring(db, within_seconds=seconds, user_id=user_id)
