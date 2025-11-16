import asyncio
import json
import os
import uuid
from unittest.mock import AsyncMock

import pytest

from api.database import (
    Database,
    create_document,
    create_job_extended,
    create_user,
    get_document,
    get_job,
    get_google_token,
    update_job_status,
    upsert_google_token,
)
from api.google_oauth import build_youtube_service_for_user
from api.protected import start_ingest_youtube_job
from core.youtube_api import fetch_video_metadata
from workers.consumer import process_ingest_youtube_job, handle_queue_message


class StubQueue:
    """Minimal queue stub for enqueue tests."""

    def __init__(self):
        self.messages = []

    async def send_generic(self, payload):
        self.messages.append(payload)
        return True


class StubQueueProducer:
    """Stub Cloudflare queue producer for retry tests."""

    def __init__(self):
        self.enqueued: list[dict] = []
        self.dlq: list[dict] = []

    async def send_generic(self, payload):
        self.enqueued.append(payload)
        return True

    async def send_to_dlq(self, job_id, error, original_message):
        self.dlq.append({"job_id": job_id, "error": error, "message": original_message})
        return True


def _parse_metadata(raw):
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def test_start_ingest_youtube_job_stores_metadata(monkeypatch):
    async def _run():
        db = Database()
        user_id = f"user-{uuid.uuid4()}"
        await create_user(db, user_id=user_id, github_id=None, email=f"{user_id}@example.com")

        stub_queue = StubQueue()
        job = None
        try:
            # Ensure external enqueue path is used
            monkeypatch.setattr("api.protected.settings.use_inline_queue", False)
            monkeypatch.setattr("api.protected.build_youtube_service_for_user", AsyncMock(return_value=object()))

            metadata_bundle = {
                "frontmatter": {"title": "Test Video", "slug": "test-video"},
                "metadata": {
                    "title": "Test Video",
                    "description": "Demo description",
                    "duration_seconds": 150,
                    "channel_title": "Test Channel",
                    "channel_id": "chan123",
                    "published_at": "2024-01-01T00:00:00Z",
                    "thumbnails": {},
                    "category_id": "24",
                    "tags": ["demo"],
                },
            }
            metadata_bundle["metadata"]["url"] = "https://youtu.be/abc12345678"

            def _fake_fetch(service, video_id):
                return metadata_bundle

            monkeypatch.setattr("api.protected.fetch_video_metadata", _fake_fetch)

            job = await start_ingest_youtube_job(db, stub_queue, user_id, "https://youtu.be/abc12345678")

            assert job.job_type == "ingest_youtube"
            assert stub_queue.messages and stub_queue.messages[0]["youtube_video_id"] == "abc12345678"

            job_row = await get_job(db, job.job_id, user_id)
            assert job_row is not None
            payload = _parse_metadata(job_row.get("payload"))
            assert payload["metadata"]["duration_seconds"] == 150
            assert payload["frontmatter"]["slug"] == "test-video"

            doc = await get_document(db, job.document_id, user_id=user_id)
            assert doc is not None
            metadata = _parse_metadata(doc.get("metadata"))
            assert metadata["youtube"]["duration_seconds"] == 150
            assert metadata["url"] == "https://youtu.be/abc12345678"
        finally:
            # cleanup (best-effort)
            try:
                if job is not None:
                    await db.execute("DELETE FROM jobs WHERE job_id = ?", (job.job_id,))
                    await db.execute("DELETE FROM documents WHERE document_id = ?", (job.document_id,))
            finally:
                await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

    asyncio.run(_run())


def test_process_ingest_youtube_job_merges_metadata(monkeypatch):
    """Test YouTube ingestion with real API calls using access token from YOUTUBE_TEST_ACCESS_TOKEN env var."""
    async def _run():
        # Get access token from environment
        access_token = os.environ.get("YOUTUBE_TEST_ACCESS_TOKEN")
        refresh_token = os.environ.get("YOUTUBE_TEST_REFRESH_TOKEN")
        if not access_token:
            pytest.skip("YOUTUBE_TEST_ACCESS_TOKEN not set - skipping real API test")
        
        # Use a real YouTube video ID that YOU OWN and has captions
        # The YouTube Captions API only works for videos owned by the authenticated user
        # You can override with YOUTUBE_TEST_VIDEO_ID env var
        video_id = os.environ.get("YOUTUBE_TEST_VIDEO_ID")
        if not video_id:
            pytest.skip("YOUTUBE_TEST_VIDEO_ID not set - must be a video you own with captions")
        
        # Note: Token must be issued with 'youtube.force-ssl' scope (not 'youtube' or 'youtube.readonly')
        # Users with youtube or youtube.readonly tokens need to re-authenticate via /auth/google/start?integration=youtube
        
        db = Database()
        user_id = f"user-{uuid.uuid4()}"
        document_id = str(uuid.uuid4())
        job_id = None
        try:
            await create_user(db, user_id=user_id, github_id=None, email=f"{user_id}@example.com")
            
            # Store Google token for YouTube integration (with refresh token if available)
            # Use explicit test scopes - youtube.force-ssl is required for captions API
            actual_scopes = "https://www.googleapis.com/auth/youtube.force-ssl"
            
            await upsert_google_token(
                db,
                user_id=user_id,
                integration="youtube",
                access_token=access_token,
                refresh_token=refresh_token,
                expiry=None,
                token_type="Bearer",
                scopes=actual_scopes,  # Use actual scopes from the real token
            )
            
            # Fetch real video metadata from YouTube API
            yt_service = await build_youtube_service_for_user(db, user_id)
            meta_bundle = await asyncio.to_thread(fetch_video_metadata, yt_service, video_id)
            youtube_meta = meta_bundle.get("metadata") or {}
            frontmatter_bundle = meta_bundle.get("frontmatter") or {}
            
            metadata = {
                "url": f"https://youtu.be/{video_id}",
                "source": "youtube",
                "youtube": youtube_meta,
            }
            frontmatter = frontmatter_bundle
            await create_document(
                db,
                document_id=document_id,
                user_id=user_id,
                source_type="youtube",
                source_ref=video_id,
                raw_text=None,
                metadata=metadata,
                frontmatter=frontmatter,
                content_format="youtube",
            )
            job_id = str(uuid.uuid4())
            # Include duration_s from fetched metadata
            payload = {
                "youtube_video_id": video_id,
                "metadata": youtube_meta,
                "frontmatter": frontmatter,
                "duration_s": youtube_meta.get("duration_seconds"),
            }
            await create_job_extended(
                db,
                job_id,
                user_id,
                job_type="ingest_youtube",
                document_id=document_id,
                payload=payload,
            )
            await update_job_status(db, job_id, "pending")

            # Only mock notify_job to avoid side effects
            monkeypatch.setattr("workers.consumer.notify_job", AsyncMock(return_value=None))

            # Use REAL API calls - no mocks for fetch_captions_text or build_youtube_service_for_user
            await process_ingest_youtube_job(db, job_id, user_id, document_id, video_id, payload)

            doc = await get_document(db, document_id, user_id=user_id)
            assert doc is not None
            
            # Verify raw_text was set from real API
            raw_text = doc.get("raw_text")
            assert raw_text is not None, "raw_text must be set from YouTube API"
            assert len(raw_text.strip()) > 0, "raw_text must not be empty"
            
            metadata = _parse_metadata(doc.get("metadata"))
            # Verify transcript metadata was set
            assert "transcript" in metadata, "transcript metadata must be present"
            assert metadata["transcript"]["chars"] == len(raw_text)
            assert metadata["latest_ingest_job_id"] == job_id

            job_row = await get_job(db, job_id, user_id)
            assert job_row is not None
            assert job_row["status"] == "completed"
            output = _parse_metadata(job_row.get("output"))
            assert output["transcript"]["duration_s"] is not None, "duration_s must be set from API"
            assert output["metadata"]["youtube"]["video_id"] == video_id
        finally:
            # cleanup regardless of test outcome
            await db.execute("DELETE FROM usage_events WHERE job_id = ?", ((job_id or ''),))
            if job_id:
                await db.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
            await db.execute("DELETE FROM google_integration_tokens WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

    asyncio.run(_run())


def test_ingest_youtube_queue_flow(monkeypatch):
    async def _run():
        db = Database()
        user_id = f"user-{uuid.uuid4()}"
        await create_user(db, user_id=user_id, github_id=None, email=f"{user_id}@example.com")

        stub_queue = StubQueue()
        job_row = None
        fake_service = object()
        fake_text = "Hello world from captions."

        try:
            monkeypatch.setattr("api.protected.settings.use_inline_queue", False)
            monkeypatch.setattr("api.protected.build_youtube_service_for_user", AsyncMock(return_value=fake_service))
            monkeypatch.setattr("workers.consumer.build_youtube_service_for_user", AsyncMock(return_value=fake_service))

            metadata_bundle = {
                "frontmatter": {"title": "Queue Flow Video", "slug": "queue-flow-video"},
                "metadata": {
                    "title": "Queue Flow Video",
                    "description": "Demo integration path",
                    "duration_seconds": 90,
                    "channel_title": "Queue Channel",
                    "channel_id": "queue123",
                    "published_at": "2024-01-01T00:00:00Z",
                    "thumbnails": {},
                    "category_id": "24",
                    "tags": ["queue", "demo"],
                    "url": "https://youtu.be/queue1234567",
                },
            }

            def _fake_fetch_metadata(service, video_id):
                assert service is fake_service
                assert video_id == "queue1234567"
                return metadata_bundle

            monkeypatch.setattr("api.protected.fetch_video_metadata", _fake_fetch_metadata)
            def _fake_fetch_captions(service, video_id, langs):
                assert service is fake_service
                assert video_id == "queue1234567"
                assert "en" in langs
                return {"success": True, "text": fake_text, "lang": "en", "source": "captions"}

            monkeypatch.setattr("workers.consumer.fetch_captions_text", _fake_fetch_captions)
            monkeypatch.setattr("workers.consumer.notify_job", AsyncMock(return_value=None))

            job_status = await start_ingest_youtube_job(
                db, stub_queue, user_id, "https://youtu.be/queue1234567"
            )
            job_row = await get_job(db, job_status.job_id, user_id)

            assert stub_queue.messages, "Queue message must be produced"
            message = stub_queue.messages[0]
            assert message["job_id"] == job_status.job_id
            assert message["document_id"] == job_status.document_id

            await handle_queue_message(message, db)

            job_row = await get_job(db, job_status.job_id, user_id)
            assert job_row["status"] == "completed"
            output = _parse_metadata(job_row.get("output"))
            assert output["transcript"]["chars"] == len(fake_text)

            doc = await get_document(db, job_status.document_id, user_id=user_id)
            assert doc is not None
            assert doc.get("raw_text") == fake_text
            metadata = _parse_metadata(doc.get("metadata"))
            assert metadata["transcript"]["duration_s"] == 90
            assert metadata["youtube"]["video_id"] == "queue1234567"
        finally:
            job_id = job_row["job_id"] if job_row else None
            await db.execute("DELETE FROM usage_events WHERE job_id = ?", ((job_id or ""),))
            if job_id:
                await db.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM documents WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

    asyncio.run(_run())


def test_ingest_youtube_retry_and_dlq(monkeypatch):
    async def _run():
        db = Database()
        user_id = f"user-{uuid.uuid4()}"
        await create_user(db, user_id=user_id, github_id=None, email=f"{user_id}@example.com")
        stub_queue = StubQueue()
        queue_producer = StubQueueProducer()

        monkeypatch.setattr("workers.consumer.settings.use_inline_queue", False)
        monkeypatch.setattr("workers.consumer.settings.max_job_retries", 2)
        monkeypatch.setattr("api.protected.settings.use_inline_queue", False)

        fake_service = object()
        monkeypatch.setattr("api.protected.build_youtube_service_for_user", AsyncMock(return_value=fake_service))
        metadata_bundle = {
            "frontmatter": {"title": "Retry Video", "slug": "retry-video"},
            "metadata": {
                "title": "Retry Video",
                "description": "Retry scenario",
                "duration_seconds": 42,
                "channel_title": "Retry",
                "channel_id": "retry123",
                "published_at": "2024-01-01T00:00:00Z",
                "thumbnails": {},
                "category_id": "24",
                "tags": ["retry"],
                "url": "https://youtu.be/queue1234567",
            },
        }

        def _fake_meta(service, video_id):
            assert service is fake_service
            return metadata_bundle

        monkeypatch.setattr("api.protected.fetch_video_metadata", _fake_meta)
        failing_process = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr("workers.consumer.process_ingest_youtube_job", failing_process)

        job_status = await start_ingest_youtube_job(
            db, stub_queue, user_id, "https://youtu.be/queue1234567"
        )
        message = stub_queue.messages[0]

        await handle_queue_message(message, db, queue_producer)
        job_row = await get_job(db, job_status.job_id, user_id)
        assert job_row["status"] == "pending"
        assert job_row["attempt_count"] == 1
        assert job_row["next_attempt_at"] is not None
        assert len(queue_producer.enqueued) == 1
        assert queue_producer.dlq == []

        await handle_queue_message(message, db, queue_producer)
        job_row = await get_job(db, job_status.job_id, user_id)
        assert job_row["status"] == "failed"
        assert job_row["attempt_count"] == 2
        assert len(queue_producer.dlq) == 1

        await db.execute("DELETE FROM jobs WHERE job_id = ?", (job_status.job_id,))
        await db.execute("DELETE FROM documents WHERE document_id = ?", (job_status.document_id,))
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

    asyncio.run(_run())
