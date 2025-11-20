import asyncio
import json
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tests.conftest import create_test_user
from src.workers.api.database import (
    Database,
    create_document,
    create_job_extended,
    get_document,
    upsert_google_token,
)
from src.workers.api.protected import start_ingest_drive_job
from src.workers.consumer import process_ingest_drive_job, process_drive_change_poll_job
from src.workers.api.drive_docs import sync_drive_doc_for_document


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
async def test_drive_change_poll_marks_external_and_triggers_ingest(monkeypatch, isolated_db):
    """Test Drive change poll with real API calls using access token from DRIVE_TEST_ACCESS_TOKEN env var."""
    # Get access token from environment (like YouTube tests do)
    access_token = os.environ.get("DRIVE_TEST_ACCESS_TOKEN")
    refresh_token = os.environ.get("DRIVE_TEST_REFRESH_TOKEN")
    if not access_token:
        pytest.skip("DRIVE_TEST_ACCESS_TOKEN not set - skipping real API test. Set env var to use real credentials.")
    
    db = isolated_db
    # Ensure notifications schema exists (required for record_pipeline_event)
    from src.workers.api.database import ensure_notifications_schema
    await ensure_notifications_schema(db)
    user_id = f"user-{uuid.uuid4()}"
    await create_test_user(db, user_id=user_id, email=f"{user_id}@example.com")
    
    # Store real Google token for Drive integration (with refresh token if available)
    # Drive integration requires both drive and docs scopes
    await upsert_google_token(
        db,
        user_id=user_id,
        integration="drive",
        access_token=access_token,
        refresh_token=refresh_token,
        expiry=None,
        token_type="Bearer",
        scopes="https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/documents",
    )
    
    # Use a real Drive file ID that exists and you have access to
    # You can override with DRIVE_TEST_FILE_ID env var
    file_id = os.environ.get("DRIVE_TEST_FILE_ID", "2" * 44)  # Default to a test pattern if not set
    
    document_id = str(uuid.uuid4())
    await create_document(
        db,
        document_id=document_id,
        user_id=user_id,
        source_type="drive",
        metadata={"drive": {"file_id": file_id}},
        drive_file_id=file_id,
        drive_revision_id="rev-1",
    )
    job_id = str(uuid.uuid4())
    await create_job_extended(
        db,
        job_id,
        user_id,
        job_type="drive_change_poll",
    )

    # Mock the ingest job to avoid actually processing
    called = AsyncMock()
    monkeypatch.setattr("src.workers.consumer.process_ingest_drive_job", called)

    # Now call with real credentials - build_drive_service_for_user will use the real token
    await process_drive_change_poll_job(db, job_id, user_id, [document_id], queue_producer=None)
    assert called.await_count == 1
    stored = await get_document(db, document_id, user_id=user_id)
    metadata = stored.get("metadata")
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["drive"]["external_edit_detected"] is True


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
