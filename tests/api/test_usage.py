from src.workers.api.database import record_usage_event, get_usage_summary, list_usage_events
import uuid
import asyncio
import pytest


@pytest.mark.asyncio
async def test_usage_events_and_summary(isolated_db):
    db = isolated_db

    user_id = f"u_{uuid.uuid4()}"
    job_id = f"j_{uuid.uuid4()}"

    # seed a few events
    await record_usage_event(db, user_id, job_id, "transcribe", {"duration_s": 120})
    await record_usage_event(db, user_id, job_id, "download", {"bytes_downloaded": 1024, "duration_s": 120})

    # list events
    items = await list_usage_events(db, user_id, limit=10, offset=0)
    assert len(items) >= 2

    # summary
    summary = await get_usage_summary(db, user_id, window_days=7)
    assert summary["events"] >= 2
    assert summary["audio_duration_s"] >= 120
    assert summary["bytes_downloaded"] >= 1024
