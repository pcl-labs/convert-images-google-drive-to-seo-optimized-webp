import os
import asyncio
import pytest

from src.workers.api.database import (
    Database,
    record_pipeline_event,
    list_pipeline_events,
)


@pytest.mark.asyncio
async def test_pipeline_events_round_trip(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(db_path))
    db = Database()
    await db.execute(
        "INSERT OR IGNORE INTO users (user_id, email) VALUES (?, ?)",
        ("user-1", "user1@example.com"),
    )
    await db.execute(
        "INSERT OR REPLACE INTO jobs (job_id, user_id, status, progress, job_type, created_at) VALUES (?, ?, 'pending', '{}', 'ingest_youtube', datetime('now'))",
        ("job-1", "user-1"),
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
    )
    events = await list_pipeline_events(db, "user-1", job_id="job-1")
    assert events
    assert events[0]["stage"] == "test"
    assert events[0]["data"].get("foo") == "bar"
