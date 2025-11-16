from __future__ import annotations

import re
from typing import Optional, Dict, Any
import asyncio

from .app_logging import get_logger
from .database import get_drive_workspace, upsert_drive_workspace
from .google_oauth import build_drive_service_for_user, build_docs_service_for_user
from src.workers.core.google_async import execute_google_request

logger = get_logger(__name__)


def _sanitize_folder_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9 _-]", "", name or "").strip()
    return clean or "Quill Document"


def _require_drive_id(entity: Optional[dict], label: str) -> str:
    if not isinstance(entity, dict):
        raise RuntimeError(f"{label} response is missing.")
    folder_id = entity.get("id")
    if not isinstance(folder_id, str) or not folder_id.strip():
        raise RuntimeError(f"{label} is missing a valid Drive ID.")
    return folder_id


async def _create_drive_folder(drive_service, name: str, parent: Optional[str] = None) -> dict:
    body: Dict[str, Any] = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "appProperties": {"quill_workspace": "true"},
    }
    if parent:
        body["parents"] = [parent]
    return await execute_google_request(
        drive_service.files().create(body=body, fields="id,name,webViewLink")
    )


async def ensure_drive_workspace(db, user_id: str):
    existing = await get_drive_workspace(db, user_id)
    if existing:
        return existing
    drive_service = await build_drive_service_for_user(db, user_id)  # type: ignore
    root = None
    try:
        query = (
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false and appProperties has { key='quill_workspace' and value='true' }"
        )
        resp = await execute_google_request(
            drive_service.files().list(q=query, pageSize=1, fields="files(id,name,webViewLink)")
        )
        files = (resp or {}).get("files") or []
        root = files[0] if files else None
    except Exception as exc:
        logger.warning(
            "failed_to_search_workspace_root",
            exc_info=True,
            extra={"error": str(exc), "hint": "drive appProperties query"},
        )
        root = None

    if not root:
        root = await _create_drive_folder(drive_service, "Quill")
    root_id = _require_drive_id(root, "Workspace root folder")
    metadata = {"root": root}
    workspace = await upsert_drive_workspace(
        db,
        user_id,
        root_id,
        root_id,
        root_id,
        metadata=metadata,
    )
    return workspace


async def _ensure_child_folder(drive_service, parent_id: str, name: str) -> dict:
    escaped_name = (name or "").replace("'", "''")
    query = (
        f"'{parent_id}' in parents and trashed = false and "
        f"mimeType = 'application/vnd.google-apps.folder' and name = '{escaped_name}'"
    )
    resp = await execute_google_request(
        drive_service.files().list(q=query, pageSize=1, fields="files(id,name,webViewLink)")
    )
    files = resp.get("files") or []
    if files:
        return files[0]
    return await _create_drive_folder(drive_service, name, parent_id)


async def _fetch_folder_meta(drive_service, folder_id: str) -> dict:
    return await execute_google_request(
        drive_service.files().get(fileId=folder_id, fields="id,name,webViewLink")
    )


async def ensure_document_drive_structure(
    db,
    user_id: str,
    *,
    name: str,
    existing_folder_id: Optional[str] = None,
) -> dict:
    workspace = await ensure_drive_workspace(db, user_id)
    drive_service = await build_drive_service_for_user(db, user_id)  # type: ignore
    docs_service = await build_docs_service_for_user(db, user_id)  # type: ignore

    if existing_folder_id:
        attempts = 3
        delay = 0.5
        last_exc: Exception | None = None
        base_folder = None
        creation_failed_after_404 = False
        for i in range(attempts):
            try:
                base_folder = await _fetch_folder_meta(drive_service, existing_folder_id)
                break
            except Exception as exc:
                last_exc = exc
                # Detect clear not-found case (prefer resp.status if present)
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status == 404:
                    try:
                        base_folder = await _create_drive_folder(
                            drive_service, _sanitize_folder_name(name), workspace["root_folder_id"]
                        )
                        break
                    except Exception as create_exc:
                        last_exc = create_exc
                        creation_failed_after_404 = True
                        logger.error(
                            "drive_folder_creation_failed_after_404",
                            exc_info=True,
                            extra={
                                "user_id": user_id,
                                "folder_id": existing_folder_id,
                                "sanitized_name": _sanitize_folder_name(name),
                            },
                        )
                        break
                # Transient error: backoff and retry
                logger.warning(
                    "drive_existing_folder_fetch_retry",
                    exc_info=True,
                    extra={"user_id": user_id, "folder_id": existing_folder_id, "attempt": i + 1},
                )
                await asyncio.sleep(delay)
                delay *= 2
        if base_folder is None:
            # Retries exhausted; surface the original failure
            if creation_failed_after_404:
                logger.error(
                    "drive_folder_creation_failed_after_404_final",
                    exc_info=True,
                    extra={
                        "user_id": user_id,
                        "folder_id": existing_folder_id,
                        "sanitized_name": _sanitize_folder_name(name),
                    },
                )
            else:
                logger.error(
                    "drive_existing_folder_fetch_failed_final",
                    exc_info=True,
                    extra={"user_id": user_id, "folder_id": existing_folder_id},
                )
            raise last_exc if last_exc else RuntimeError("Failed to fetch existing Drive folder")
    else:
        base_folder = await _create_drive_folder(
            drive_service, _sanitize_folder_name(name), workspace["root_folder_id"]
        )

    base_folder_id = _require_drive_id(base_folder, "Document workspace folder")
    media_folder = await _ensure_child_folder(drive_service, base_folder_id, "Media")
    media_folder_id = _require_drive_id(media_folder, "Document media folder")

    doc_title = _sanitize_folder_name(name)
    created_doc = await execute_google_request(docs_service.documents().create(body={"title": doc_title}))
    if not isinstance(created_doc, dict):
        raise RuntimeError(f"Docs create returned unexpected response type: {type(created_doc)!r}")
    doc_id = created_doc.get("documentId")
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise RuntimeError("Failed to create Google Doc for Drive workspace: missing documentId")

    drive_doc_meta = await execute_google_request(
        drive_service.files().update(
            fileId=doc_id,
            addParents=base_folder_id,
            removeParents="root",
            fields="id, headRevisionId, webViewLink",
        )
    )
    if not isinstance(drive_doc_meta, dict) or "id" not in drive_doc_meta:
        raise RuntimeError(f"Drive update returned unexpected response: {drive_doc_meta!r}")
    drive_file_id = drive_doc_meta.get("id")
    if not isinstance(drive_file_id, str) or not drive_file_id.strip():
        raise RuntimeError("Drive file update did not return a valid 'id'.")

    return {
        "folder": base_folder,
        "media": media_folder,
        "file": {
            "id": drive_file_id,
            "revision_id": drive_doc_meta.get("headRevisionId"),
            "webViewLink": drive_doc_meta.get("webViewLink"),
        },
    }
