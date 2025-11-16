from __future__ import annotations

import re
from typing import Optional, Dict, Any

from .app_logging import get_logger
from .database import get_drive_workspace, upsert_drive_workspace
from .google_oauth import build_drive_service_for_user, build_docs_service_for_user
from core.google_async import execute_google_request

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
    root = await _create_drive_folder(drive_service, "Quill")
    root_id = _require_drive_id(root, "Workspace root folder")
    drafts = await _create_drive_folder(drive_service, "Drafts", root_id)
    drafts_id = _require_drive_id(drafts, "Workspace drafts folder")
    published = await _create_drive_folder(drive_service, "Published", root_id)
    published_id = _require_drive_id(published, "Workspace published folder")
    metadata = {"root": root, "drafts": drafts, "published": published}
    workspace = await upsert_drive_workspace(
        db,
        user_id,
        root_id,
        drafts_id,
        published_id,
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
        try:
            base_folder = await _fetch_folder_meta(drive_service, existing_folder_id)
        except Exception as exc:
            logger.warning(
                "drive_existing_folder_fetch_failed",
                exc_info=True,
                extra={"user_id": user_id, "folder_id": existing_folder_id, "error": str(exc)},
            )
            base_folder = await _create_drive_folder(
                drive_service, _sanitize_folder_name(name), workspace["root_folder_id"]
            )
    else:
        base_folder = await _create_drive_folder(
            drive_service, _sanitize_folder_name(name), workspace["root_folder_id"]
        )

    base_folder_id = _require_drive_id(base_folder, "Document workspace folder")
    drafts_folder = await _ensure_child_folder(drive_service, base_folder_id, "Drafts")
    drafts_folder_id = _require_drive_id(drafts_folder, "Document drafts folder")
    media_folder = await _ensure_child_folder(drive_service, base_folder_id, "Media")
    media_folder_id = _require_drive_id(media_folder, "Document media folder")
    published_folder = await _ensure_child_folder(drive_service, base_folder_id, "Published")
    published_folder_id = _require_drive_id(published_folder, "Document published folder")

    doc_title = _sanitize_folder_name(name)
    created_doc = await execute_google_request(docs_service.documents().create(body={"title": doc_title}))
    doc_id = created_doc.get("documentId")
    if not isinstance(doc_id, str) or not doc_id:
        raise RuntimeError("Failed to create Google Doc for Drive workspace.")

    drive_doc_meta = await execute_google_request(
        drive_service.files().update(
            fileId=doc_id,
            addParents=drafts_folder_id,
            removeParents="root",
            fields="id, headRevisionId, webViewLink",
        )
    )
    drive_file_id = drive_doc_meta.get("id")
    if not isinstance(drive_file_id, str) or not drive_file_id:
        raise RuntimeError("Drive file update did not return an ID.")

    return {
        "folder": base_folder,
        "drafts": drafts_folder,
        "media": media_folder,
        "published": published_folder,
        "file": {
            "id": drive_file_id,
            "revision_id": drive_doc_meta.get("headRevisionId"),
            "webViewLink": drive_doc_meta.get("webViewLink"),
        },
    }
