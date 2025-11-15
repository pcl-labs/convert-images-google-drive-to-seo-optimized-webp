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
from workers.consumer import process_ingest_youtube_job


class StubQueue:
    """Minimal queue stub for enqueue tests."""

    def __init__(self):
        self.messages = []

    async def send_generic(self, payload):
        self.messages.append(payload)
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
            # Use explicit test scopes to avoid depending on another user's token
            actual_scopes = "https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/youtube.readonly"
            
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
