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
    drafts = await _create_drive_folder(drive_service, "Drafts", root.get("id"))
    published = await _create_drive_folder(drive_service, "Published", root.get("id"))
    metadata = {"root": root, "drafts": drafts, "published": published}
    workspace = await upsert_drive_workspace(
        db,
        user_id,
        root.get("id"),
        drafts.get("id"),
        published.get("id"),
        metadata=metadata,
    )
    return workspace


async def _ensure_child_folder(drive_service, parent_id: str, name: str) -> dict:
    query = (
        f"'{parent_id}' in parents and trashed = false and "
        f"mimeType = 'application/vnd.google-apps.folder' and name = '{name}'"
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
        except Exception:
            base_folder = await _create_drive_folder(
                drive_service, _sanitize_folder_name(name), workspace["root_folder_id"]
            )
    else:
        base_folder = await _create_drive_folder(
            drive_service, _sanitize_folder_name(name), workspace["root_folder_id"]
        )

    drafts_folder = await _ensure_child_folder(drive_service, base_folder["id"], "Drafts")
    media_folder = await _ensure_child_folder(drive_service, base_folder["id"], "Media")
    published_folder = await _ensure_child_folder(drive_service, base_folder["id"], "Published")

    doc_title = _sanitize_folder_name(name)
    created_doc = await execute_google_request(docs_service.documents().create(body={"title": doc_title}))
    doc_id = created_doc.get("documentId")

    drive_doc_meta = await execute_google_request(
        drive_service.files().update(
            fileId=doc_id,
            addParents=drafts_folder["id"],
            removeParents="root",
            fields="id, headRevisionId, webViewLink",
        )
    )

    return {
        "folder": base_folder,
        "drafts": drafts_folder,
        "media": media_folder,
        "published": published_folder,
        "file": {
            "id": drive_doc_meta.get("id"),
            "revision_id": drive_doc_meta.get("headRevisionId"),
            "webViewLink": drive_doc_meta.get("webViewLink"),
        },
    }
