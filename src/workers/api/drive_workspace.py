from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from .app_logging import get_logger
from .database import (
    Database,
    get_document,
    get_drive_workspace,
    record_pipeline_event,
    update_document,
    upsert_drive_workspace,
)
from .google_oauth import build_docs_service_for_user, build_drive_service_for_user
from src.workers.core.google_async import execute_google_request

logger = get_logger(__name__)


def _json_dict_field(value: Any, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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


async def _emit_pipeline_event(
    db,
    user_id: str,
    job_id: Optional[str],
    *,
    stage: str,
    status: str,
    message: str,
    data: Optional[Dict[str, Any]] = None,
    event_type: str = "drive_workspace",
):
    if not job_id:
        return
    try:
        await record_pipeline_event(
            db,
            user_id,
            job_id,
            event_type=event_type,
            stage=stage,
            status=status,
            message=message,
            data=data or {},
        )
    except Exception:
        logger.debug("drive_workspace_pipeline_event_failed", exc_info=True)


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
    job_id: Optional[str] = None,
    event_type: str = "drive_workspace",
) -> dict:
    workspace = await ensure_drive_workspace(db, user_id)
    drive_service = await build_drive_service_for_user(db, user_id)  # type: ignore
    docs_service = await build_docs_service_for_user(db, user_id)  # type: ignore

    sanitized = _sanitize_folder_name(name)
    await _emit_pipeline_event(
        db,
        user_id,
        job_id,
        stage="drive.workspace.ensure",
        status="running",
        message="Ensuring Drive workspace exists",
        data={"document_name": sanitized, "workspace_root": workspace.get("root_folder_id")},
        event_type=event_type,
    )

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
                            drive_service, sanitized, workspace["root_folder_id"]
                        )
                        break
                    except Exception as create_exc:
                        last_exc = create_exc
                        creation_failed_after_404 = True
                        logger.error(
                            "drive_folder_creation_failed_after_404",
                            exc_info=True,
                            extra={
                                "workspace_label": "document_folder",
                                "sanitized_name": _sanitize_folder_name(name),
                            },
                        )
                        break
                # Transient error: backoff and retry
                logger.warning(
                    "drive_existing_folder_fetch_retry",
                    exc_info=True,
                    extra={"workspace_label": "document_folder", "attempt": i + 1},
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
                        "workspace_label": "document_folder",
                        "sanitized_name": _sanitize_folder_name(name),
                    },
                )
                # Surface a clear error message indicating create-after-404 failure
                raise RuntimeError(
                    "Failed to create Drive folder after 404 when ensuring document workspace"
                ) from last_exc
            else:
                logger.error(
                    "drive_existing_folder_fetch_failed_final",
                    exc_info=True,
                    extra={"workspace_label": "document_folder"},
                )
                # Surface a clear error message indicating fetch failure
                raise RuntimeError(
                    "Failed to fetch existing Drive folder when ensuring document workspace"
                ) from last_exc
    else:
        base_folder = await _create_drive_folder(
            drive_service, sanitized, workspace["root_folder_id"]
        )
        await _emit_pipeline_event(
            db,
            user_id,
            job_id,
            stage="drive.folder.create",
            status="completed",
            message="Created Drive folder for document",
            data={"folder_id": base_folder.get("id"), "document_name": sanitized},
            event_type=event_type,
        )

    base_folder_id = _require_drive_id(base_folder, "Document workspace folder")
    media_folder = await _ensure_child_folder(drive_service, base_folder_id, "Media")
    media_folder_id = _require_drive_id(media_folder, "Document media folder")

    doc_title = sanitized
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

    await _emit_pipeline_event(
        db,
        user_id,
        job_id,
        stage="drive.doc.create",
        status="completed",
        message="Google Doc created",
        data={"drive_file_id": drive_file_id, "folder_id": base_folder_id},
        event_type=event_type,
    )

    return {
        "folder": base_folder,
        "media": media_folder,
        "file": {
            "id": drive_file_id,
            "revision_id": drive_doc_meta.get("headRevisionId"),
            "webViewLink": drive_doc_meta.get("webViewLink"),
        },
    }


async def link_document_drive_workspace(
    db,
    *,
    user_id: str,
    document_id: str,
    document_name: str,
    metadata: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
    event_type: str = "drive_workspace",
) -> Dict[str, Any]:
    """
    Ensure a Drive workspace exists for a document and persist the resulting metadata.

    Args:
        db: Database handle
        user_id: Owner of the document
        document_id: Document identifier
        document_name: Preferred display name for Drive folder/Doc
        metadata: Optional metadata snapshot to merge drive info into
        job_id: Optional pipeline job id for instrumentation
    """
    doc_meta = _json_dict_field(metadata, {})
    if not doc_meta:
        existing_doc = await get_document(db, document_id, user_id=user_id)
        if existing_doc:
            doc_meta = _json_dict_field(existing_doc.get("metadata"), {})
    drive_block = _json_dict_field(doc_meta.get("drive"), {})
    existing_folder_id = (
        drive_block.get("folder_id")
        or drive_block.get("folder", {}).get("id")
        or doc_meta.get("drive_folder_id")
    )

    try:
        structure = await ensure_document_drive_structure(
            db,
            user_id,
            name=document_name,
            existing_folder_id=existing_folder_id,
            job_id=job_id,
            event_type=event_type,
        )
    except Exception as exc:
        await _emit_pipeline_event(
            db,
            user_id,
            job_id,
            stage="drive.workspace.ensure",
            status="error",
            message=f"Drive workspace linking failed: {exc}",
            data={"document_id": document_id},
            event_type=event_type,
        )
        raise

    folder = structure.get("folder") or {}
    media = structure.get("media") or {}
    file_info = structure.get("file") or {}

    now_iso = datetime.now(timezone.utc).isoformat()
    drive_block.update(
        {
            "folder": folder,
            "folder_id": folder.get("id"),
            "media": media,
            "media_folder_id": media.get("id"),
            "file_id": file_info.get("id"),
            "revision_id": file_info.get("revision_id"),
            "web_view_link": file_info.get("webViewLink"),
            "stage": drive_block.get("stage") or "draft",
        }
    )
    drive_block.setdefault("last_ingested_at", now_iso)
    drive_block.setdefault("last_ingested_revision", file_info.get("revision_id"))
    drive_block["external_edit_detected"] = False
    drive_block.pop("pending_revision_id", None)
    drive_block.pop("pending_modified_time", None)
    doc_meta["drive"] = drive_block

    await update_document(
        db,
        document_id,
        {
            "metadata": doc_meta,
            "drive_folder_id": folder.get("id"),
            "drive_media_folder_id": media.get("id"),
            "drive_file_id": file_info.get("id"),
            "drive_revision_id": file_info.get("revision_id"),
        },
    )

    await _emit_pipeline_event(
        db,
        user_id,
        job_id,
        stage="drive.workspace.link",
        status="completed",
        message="Document linked to Drive workspace",
        data={
            "document_id": document_id,
            "drive_file_id": file_info.get("id"),
            "folder_id": folder.get("id"),
        },
        event_type=event_type,
    )
    return drive_block


class DriveWorkspaceSyncService:
    """Detect Drive changes and schedule follow-up ingest jobs."""

    def __init__(
        self,
        db: Database,
        user_id: str,
        *,
        job_id: Optional[str] = None,
        event_type: str = "drive_sync",
    ):
        self.db = db
        self.user_id = user_id
        self.job_id = job_id
        self.event_type = event_type

    async def _emit(self, stage: str, status: str, message: str, data: Optional[Dict[str, Any]] = None):
        await _emit_pipeline_event(
            self.db,
            self.user_id,
            self.job_id,
            stage=stage,
            status=status,
            message=message,
            data=data,
            event_type=self.event_type,
        )

    async def _load_documents(self, document_ids: Optional[Iterable[str]]) -> List[Dict[str, Any]]:
        params: List[Any] = [self.user_id]
        if document_ids:
            doc_ids = [doc_id for doc_id in document_ids if doc_id]
            if not doc_ids:
                return []
            placeholders = ",".join(["?"] * len(doc_ids))
            query = f"SELECT * FROM documents WHERE user_id = ? AND drive_file_id IS NOT NULL AND document_id IN ({placeholders})"
            params.extend(doc_ids)
        else:
            query = "SELECT * FROM documents WHERE user_id = ? AND drive_file_id IS NOT NULL"
        rows = await self.db.execute_all(query, tuple(params))
        return [dict(row) for row in rows or []]

    async def _mark_pending_revision(
        self,
        document: Dict[str, Any],
        revision_id: str,
        modified_time: Optional[str],
    ) -> None:
        metadata = _json_dict_field(document.get("metadata"), {})
        drive_block = _json_dict_field(metadata.get("drive"), {})
        drive_block.update(
            {
                "external_edit_detected": True,
                "pending_revision_id": revision_id,
                "pending_modified_time": modified_time,
            }
        )
        metadata["drive"] = drive_block
        await update_document(self.db, document.get("document_id"), {"metadata": metadata})

    async def scan_for_changes(
        self,
        document_ids: Optional[Iterable[str]] = None,
        on_change: Optional[Callable[[Dict[str, Any], str, Dict[str, Any]], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        await self._emit("drive.sync.scan", "running", "Scanning Drive documents for changes")
        documents = await self._load_documents(document_ids)
        if not documents:
            await self._emit("drive.sync.scan", "completed", "No Drive-backed documents to inspect")
            return {"documents_checked": 0, "changes": 0}

        try:
            drive_service = await build_drive_service_for_user(self.db, self.user_id)
        except Exception as exc:
            await self._emit(
                "drive.sync.error",
                "error",
                "Drive not linked or access denied",
                data={"error": str(exc)},
            )
            raise
        changed: List[Dict[str, Any]] = []

        for document in documents:
            file_id = document.get("drive_file_id")
            if not file_id:
                continue
            try:
                drive_meta = await execute_google_request(
                    drive_service.files().get(fileId=file_id, fields="id, headRevisionId, modifiedTime")
                )
            except Exception as exc:
                logger.error(
                    "drive_sync_fetch_failed",
                    exc_info=True,
                    extra={"document_id": document.get("document_id"), "file_id": file_id},
                )
                await self._emit(
                    "drive.sync.error",
                    "error",
                    f"Failed to fetch Drive metadata for {file_id}",
                    data={"document_id": document.get("document_id"), "error": str(exc)},
                )
                continue
            revision_id = (drive_meta or {}).get("headRevisionId")
            if not revision_id:
                continue
            if revision_id == document.get("drive_revision_id"):
                continue
            await self._mark_pending_revision(document, revision_id, (drive_meta or {}).get("modifiedTime"))
            changed.append(document)
            await self._emit(
                "drive.sync.detected",
                "running",
                "Detected external Drive edits",
                data={"document_id": document.get("document_id"), "drive_file_id": file_id, "revision_id": revision_id},
            )
            if on_change:
                await on_change(document, revision_id, drive_meta or {})

        await self._emit(
            "drive.sync.scan",
            "completed",
            "Drive sync completed",
            data={"documents_checked": len(documents), "changes": len(changed)},
        )
        return {"documents_checked": len(documents), "changes": len(changed)}
