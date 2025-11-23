import os
import json
import asyncio
import pytest

from src.workers.api.database import (
    Database,
    record_pipeline_event,
    list_pipeline_events,
    ensure_notifications_schema,
    create_document,
)
from src.workers.api.config import settings as global_settings


@pytest.mark.asyncio
async def test_pipeline_events_round_trip(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("ENABLE_NOTIFICATIONS", "true")
    monkeypatch.setattr(global_settings, "enable_notifications", True, raising=False)
    db = Database()
    await ensure_notifications_schema(db)
    await db.execute(
        "INSERT OR IGNORE INTO users (user_id, email) VALUES (?, ?)",
        ("user-1", "user1@example.com"),
    )
    await db.execute(
        "INSERT OR REPLACE INTO jobs (job_id, user_id, status, progress, job_type, created_at) VALUES (?, ?, 'pending', '{}', 'ingest_youtube', datetime('now'))",
        ("job-1", "user-1"),
    )
    await create_document(
        db,
        document_id="doc-1",
        user_id="user-1",
        source_type="text",
        raw_text="hello world",
        metadata={"title": "Doc Title", "drive": {"file_id": "drive-1", "web_view_link": "https://docs.example/doc-1"}},
        frontmatter={"slug": "doc-title"},
    )
    await db.execute(
        "UPDATE jobs SET session_id = ?, document_id = ? WHERE job_id = ?",
        ("sess-1", "doc-1", "job-1"),
    )
    await record_pipeline_event(
        db,
        "user-1",
        "job-1",
        event_type="ingest_youtube",
        stage="test",
        status="running",
        message="pipeline event recorded",
        data={"foo": "bar"},
        notify_level="info",
        notify_text="Test pipeline notification",
        notify_context={"document_id": "doc-1"},
    )
    events = await list_pipeline_events(db, "user-1", job_id="job-1")
    assert events
    event = events[0]
    assert event["stage"] == "test"
    payload = event["data"]
    assert payload.get("foo") == "bar"
    assert payload.get("document_id") == "doc-1"
    assert payload.get("document_title") == "Doc Title"
    assert payload.get("drive_file_id") == "drive-1"
    assert payload.get("drive_web_view_link") == "https://docs.example/doc-1"
    assert event["session_id"] == "sess-1"
    filtered = await list_pipeline_events(db, "user-1", session_id="sess-1")
    assert filtered
    assert filtered[0]["event_id"] == events[0]["event_id"]
    assert await list_pipeline_events(db, "user-1", session_id="missing") == []
    rows = await db.execute_all("SELECT * FROM notifications WHERE user_id = ?", ("user-1",))
    assert rows is not None
    notifications = [dict(row) for row in rows]
    assert len(notifications) == 1
    notif = notifications[0]
    assert notif.get("level") == "info"
    assert notif.get("text") == "Test pipeline notification"
    assert notif.get("user_id") == "user-1"
    assert notif.get("event_id") is not None
    assert notif.get("created_at") is not None
    context = notif.get("context")
    if isinstance(context, str):
        context = json.loads(context)
    assert isinstance(context, dict)
    assert context.get("document_id") == "doc-1"
