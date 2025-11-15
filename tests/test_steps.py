import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from api.database import Database, create_document, get_document
from api.deps import set_db_instance, get_current_user
from api.main import app


@pytest.fixture()
def steps_client():
    db = Database()
    set_db_instance(db)
    user = {"user_id": "steps-user", "email": "steps@example.com"}

    async def _seed_document() -> str:
        document_id = str(uuid.uuid4())
        await create_document(
            db,
            document_id=document_id,
            user_id=user["user_id"],
            source_type="text",
            source_ref=None,
            raw_text="Paragraph one.\nParagraph two.\nParagraph three.",
            metadata={"title": "Seed"},
        )
        return document_id

    document_id = asyncio.run(_seed_document())
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    try:
        yield client, user, document_id
    finally:
        async def _cleanup():
            try:
                await db.execute("DELETE FROM step_invocations WHERE user_id = ?", (user["user_id"],))
            except Exception:
                pass
        asyncio.run(_cleanup())
        client.close()
        app.dependency_overrides.clear()


def test_outline_generate_idempotency(steps_client):
    client, _, document_id = steps_client
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    resp = client.post("/api/v1/steps/outline.generate", json={"document_id": document_id}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "outline" in data
    assert data["document_id"] == document_id
    resp_repeat = client.post("/api/v1/steps/outline.generate", json={"document_id": document_id}, headers=headers)
    assert resp_repeat.status_code == 200
    assert resp_repeat.json() == data
    conflict = client.post(
        "/api/v1/steps/outline.generate",
        json={"text": "different content"},
        headers=headers,
    )
    assert conflict.status_code == 409


def test_document_persist_updates_raw_text(steps_client):
    client, user, document_id = steps_client
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    payload = {"document_id": document_id, "raw_text": "updated text"}
    resp = client.post("/api/v1/steps/document.persist", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["updated"] == ["raw_text"]

    async def _fetch_doc():
        db = Database()
        return await get_document(db, document_id, user_id=user["user_id"])

    updated = asyncio.run(_fetch_doc())
    assert updated is not None
    assert updated.get("raw_text") == "updated text"


def test_idempotency_header_required(steps_client):
    client, _, document_id = steps_client
    resp = client.post("/api/v1/steps/outline.generate", json={"document_id": document_id})
    assert resp.status_code == 400
