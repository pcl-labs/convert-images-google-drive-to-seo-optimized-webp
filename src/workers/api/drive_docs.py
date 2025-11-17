from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Dict, List, Optional

from .app_logging import get_logger
from .database import Database, get_document, update_document
from .google_oauth import build_docs_service_for_user, build_drive_service_for_user
from src.workers.core.google_async import execute_google_request

logger = get_logger(__name__)


def dict_from_field(value: Any, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return dict(default or {})
    if isinstance(value, dict):
        return value
    return dict(default or {})


def merge_metadata_for_updates(
    document_metadata: Dict[str, Any],
    incoming: Optional[Dict[str, Any]],
    status: Optional[str],
) -> Dict[str, Any]:
    merged = dict(document_metadata or {})
    if isinstance(incoming, dict):
        merged.update(incoming)
    if status:
        merged["drive_sync_status"] = status
    return merged


def _build_drive_update_requests(end_index: int, text: str) -> List[Dict[str, Any]]:
    requests: List[Dict[str, Any]] = []
    if end_index > 1:
        requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index}}})
    requests.append({"insertText": {"location": {"index": 1}, "text": text}})
    return requests


def _calculate_drive_stage(
    updates: Dict[str, Any],
    drive_block: Dict[str, Any],
    metadata: Dict[str, Any],
) -> str:
    incoming_meta = updates.get("metadata") if isinstance(updates.get("metadata"), dict) else {}
    if isinstance(incoming_meta, dict):
        stage = incoming_meta.get("drive_stage")
        if stage:
            return stage
    if isinstance(drive_block, dict):
        stage = drive_block.get("stage")
        if stage:
            return stage
    return metadata.get("drive_stage") or "draft"


async def retry_update_document(
    db: Database,
    document_id: str,
    payload: Dict[str, Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.2,
) -> None:
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            await update_document(db, document_id, payload)
            return
        except Exception:
            if attempt == max_attempts:
                logger.error(
                    "drive_docs_update_document_failed",
                    extra={"document_id": document_id},
                )
                raise
            await asyncio.sleep(delay + random.uniform(0, 0.1))
            delay *= 2


async def schedule_drive_reconcile_job(
    db: Database,
    document_id: str,
    user_id: str,
    drive_file_id: Optional[str],
    metadata_snapshot: Optional[Dict[str, Any]] = None,
) -> None:
    logger.warning(
        "drive_docs_reconcile_scheduled",
        extra={"document_id": document_id, "drive_file_id": drive_file_id},
    )
    try:
        if metadata_snapshot is None:
            doc = await get_document(db, document_id, user_id=user_id)
            metadata_snapshot = dict_from_field(doc.get("metadata") if doc else {})
        metadata_copy = dict(metadata_snapshot or {})
        metadata_copy["drive_sync_status"] = "failed"
        metadata_copy["drive_reconcile_required"] = True
        await update_document(
            db,
            document_id,
            {"metadata": metadata_copy},
        )
    except Exception:
        logger.exception(
            "drive_docs_reconcile_flag_failed",
            extra={"document_id": document_id},
        )


async def sync_drive_doc_after_persist(
    db: Database,
    user_id: str,
    document: Dict[str, Any],
    updates: Dict[str, Any],
) -> None:
    metadata = dict_from_field(document.get("metadata"))
    drive_block = metadata.get("drive") if isinstance(metadata, dict) else {}
    if not isinstance(drive_block, dict):
        drive_block = {}
    drive_file_id = document.get("drive_file_id") or drive_block.get("file_id")
    if not drive_file_id:
        return
    new_text = updates.get("raw_text")
    if new_text is None:
        new_text = document.get("raw_text")
    if new_text is None:
        return
    try:
        docs_service = await build_docs_service_for_user(db, user_id)
        drive_service = await build_drive_service_for_user(db, user_id)
    except ValueError as exc:
        logger.warning("drive_docs_unlinked", extra={"document_id": document.get("document_id"), "error": str(exc)})
        return
    try:
        current_doc = await execute_google_request(docs_service.documents().get(documentId=drive_file_id))
        body_content = (current_doc.get("body", {}) or {}).get("content", []) or []
        end_index = body_content[-1].get("endIndex", len(new_text) + 1) if body_content else len(new_text) + 1
    except Exception:
        logger.exception("drive_docs_get_failed", extra={"drive_file_id": drive_file_id, "document_id": document.get("document_id")})
        end_index = len(new_text) + 1
    requests = _build_drive_update_requests(end_index, new_text)
    try:
        await execute_google_request(
            docs_service.documents().batchUpdate(documentId=drive_file_id, body={"requests": requests})
        )
        drive_meta = await execute_google_request(
            drive_service.files().get(fileId=drive_file_id, fields='id, headRevisionId, parents')
        )
    except Exception:
        logger.exception(
            "drive_docs_sync_failed",
            extra={"drive_file_id": drive_file_id, "document_id": document.get("document_id")},
        )
        return
    desired_stage = _calculate_drive_stage(updates, drive_block, metadata)
    metadata["drive_stage"] = desired_stage
    metadata["drive_sync_status"] = "synced"
    drive_block.update(
        {
            "revision_id": drive_meta.get("headRevisionId"),
            "external_edit_detected": False,
            "stage": desired_stage,
        }
    )
    metadata["drive"] = drive_block
    logger.info(
        "drive_docs_pre_update",
        extra={
            "document_id": document.get("document_id"),
            "drive_file_id": drive_file_id,
            "revision_id": drive_meta.get("headRevisionId"),
            "desired_stage": desired_stage,
        },
    )
    try:
        await retry_update_document(
            db,
            document.get("document_id"),
            {
                "metadata": metadata,
                "drive_revision_id": drive_meta.get("headRevisionId"),
            },
        )
    except Exception:
        await schedule_drive_reconcile_job(
            db,
            document.get("document_id"),
            user_id,
            drive_file_id,
            metadata_snapshot=metadata,
        )
        raise


async def sync_drive_doc_for_document(
    db: Database,
    user_id: str,
    document_id: str,
    updates: Dict[str, Any],
) -> None:
    document = await get_document(db, document_id, user_id=user_id)
    if not document:
        raise ValueError("Document not found")
    await sync_drive_doc_after_persist(db, user_id, document, updates)
