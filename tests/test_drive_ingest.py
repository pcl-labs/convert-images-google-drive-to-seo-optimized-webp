import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.workers.api.database import (
    Database,
    create_user,
    create_document,
    create_job_extended,
    get_document,
)
from src.workers.api.protected import start_ingest_drive_job
from src.workers.consumer import process_ingest_drive_job, process_drive_change_poll_job
from src.workers.api.drive_docs import sync_drive_doc_for_document
from tests.conftest import create_test_user


class _StubRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _StubDocsService:
    def __init__(self, text):
        self.text = text
        self.updates = []

    def documents(self):
        return self

    def get(self, documentId):
        body = {
            "documentId": documentId,
            "title": "Drive Title",
            "body": {
                "content": [
                    {
                        "endIndex": len(self.text) + 1,
                        "paragraph": {
                            "elements": [
                                {"textRun": {"content": self.text + "\n"}}
                            ]
                        },
                    }
                ]
            },
        }
        return _StubRequest(body)

    def batchUpdate(self, documentId, body):
        self.updates.append({"documentId": documentId, **body})
        return _StubRequest({"status": "ok"})

    def create(self, body):
        return _StubRequest({"documentId": "new-doc"})


class _StubDriveService:
    def __init__(self, revision):
        self.revision = revision

    def files(self):
        return self

    def get(self, fileId, fields):
        return _StubRequest({
            "id": fileId,
            "headRevisionId": self.revision,
            "webViewLink": f"https://docs.google.com/document/d/{fileId}/edit",
            "modifiedTime": "2024-01-01T00:00:00Z",
        })

    def update(self, *args, **kwargs):
        return _StubRequest({"status": "moved"})


@pytest.mark.asyncio
async def test_start_ingest_drive_job_requires_file(isolated_db):
    db = isolated_db
    user_id = f"user-{uuid.uuid4()}"
    await create_test_user(db, user_id=user_id, email=f"{user_id}@example.com")
    document_id = str(uuid.uuid4())
    await create_document(
        db,
        document_id=document_id,
        user_id=user_id,
        source_type="text",
        metadata={},
    )
    queue = None
    with pytest.raises(HTTPException):
        await start_ingest_drive_job(db, queue, user_id, document_id)


@pytest.mark.asyncio
async def test_process_ingest_drive_job_persists_text(monkeypatch, isolated_db):
    db = isolated_db
    user_id = f"user-{uuid.uuid4()}"
    await create_test_user(db, user_id=user_id, email=f"{user_id}@example.com")
    document_id = str(uuid.uuid4())
    file_id = "1" * 44
    await create_document(
        db,
        document_id=document_id,
        user_id=user_id,
        source_type="drive",
        metadata={"drive": {"file_id": file_id}},
        drive_file_id=file_id,
    )
    job_id = str(uuid.uuid4())
    await create_job_extended(
        db,
        job_id,
        user_id,
        job_type="ingest_drive",
        document_id=document_id,
        payload={"drive_file_id": file_id},
    )

    docs_stub = _StubDocsService("Hello from Drive")
    drive_stub = _StubDriveService("rev-2")
    monkeypatch.setattr(
        "src.workers.consumer.build_docs_service_for_user",
        AsyncMock(return_value=docs_stub),
    )
    monkeypatch.setattr(
        "src.workers.consumer.build_drive_service_for_user",
        AsyncMock(return_value=drive_stub),
    )
    monkeypatch.setattr("src.workers.consumer.notify_job", AsyncMock(return_value=None))

    await process_ingest_drive_job(db, job_id, user_id, document_id, file_id)
    stored = await get_document(db, document_id, user_id=user_id)
    assert stored.get("raw_text") == "Hello from Drive"
    assert stored.get("drive_revision_id") == "rev-2"



@pytest.mark.asyncio
async def test_sync_drive_doc_for_document_updates_drive(monkeypatch, isolated_db):
    db = isolated_db
    user_id = f"user-{uuid.uuid4()}"
    await create_test_user(db, user_id=user_id, email=f"{user_id}@example.com")
    document_id = str(uuid.uuid4())
    file_id = "4" * 44
    await create_document(
        db,
        document_id=document_id,
        user_id=user_id,
        source_type="text",
        metadata={"drive": {"file_id": file_id}},
        drive_file_id=file_id,
        raw_text="Seed text",
    )

    docs_stub = _StubDocsService("Previous text")
    drive_stub = _StubDriveService("rev-sync")
    monkeypatch.setattr(
        "src.workers.api.drive_docs.build_docs_service_for_user",
        AsyncMock(return_value=docs_stub),
    )
    monkeypatch.setattr(
        "src.workers.api.drive_docs.build_drive_service_for_user",
        AsyncMock(return_value=drive_stub),
    )

    await sync_drive_doc_for_document(
        db,
        user_id,
        document_id,
        {"metadata": {"drive_stage": "outline"}},
    )
    stored = await get_document(db, document_id, user_id=user_id)
    assert stored.get("drive_revision_id") == "rev-sync"
    metadata = stored.get("metadata")
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["drive"]["stage"] == "outline"
    assert docs_stub.updates, "Expected Docs batchUpdate call"
    inserted = docs_stub.updates[0]["requests"][-1]["insertText"]["text"]
    assert inserted == "Seed text"
