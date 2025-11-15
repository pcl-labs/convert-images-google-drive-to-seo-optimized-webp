import asyncio
import json
import uuid
from unittest.mock import AsyncMock

from api.database import (
    Database,
    create_document,
    create_job_extended,
    create_user,
    get_document,
    get_job,
    update_job_status,
)
from api.protected import start_ingest_youtube_job
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
    async def _run():
        db = Database()
        user_id = f"user-{uuid.uuid4()}"
        document_id = str(uuid.uuid4())
        job_id = None
        try:
            await create_user(db, user_id=user_id, github_id=None, email=f"{user_id}@example.com")
            video_id = "xyz987654321"
            metadata = {
                "url": "https://youtu.be/xyz987654321",
                "source": "youtube",
                "youtube": {
                    "video_id": video_id,
                    "duration_seconds": 200,
                    "title": "Existing Title",
                },
            }
            frontmatter = {"title": "Existing Title"}
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
            payload = {
                "youtube_video_id": video_id,
                "metadata": metadata["youtube"],
                "frontmatter": frontmatter,
                "duration_s": 200,
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

            def _fake_transcript(video_id_arg, langs):
                return {
                    "success": True,
                    "text": "Hello world transcript",
                    "source": "captions",
                    "lang": "en",
                    "duration_s": 210,
                    "bytes_downloaded": 1024,
                }

            monkeypatch.setattr("workers.consumer.fetch_transcript_with_fallback", _fake_transcript)
            monkeypatch.setattr("workers.consumer.notify_job", AsyncMock(return_value=None))

            await process_ingest_youtube_job(db, job_id, user_id, document_id, video_id, payload)

            doc = await get_document(db, document_id, user_id=user_id)
            assert doc is not None
            assert doc.get("raw_text") == "Hello world transcript"
            metadata = _parse_metadata(doc.get("metadata"))
            assert metadata["youtube"]["duration_seconds"] == 210
            assert metadata["transcript"]["chars"] == len("Hello world transcript")
            assert metadata["latest_ingest_job_id"] == job_id

            job_row = await get_job(db, job_id, user_id)
            assert job_row is not None
            assert job_row["status"] == "completed"
            output = _parse_metadata(job_row.get("output"))
            assert output["transcript"]["duration_s"] == 210
            assert output["metadata"]["youtube"]["video_id"] == video_id
        finally:
            # cleanup regardless of test outcome
            await db.execute("DELETE FROM usage_events WHERE job_id = ?", ((job_id or ''),))
            if job_id:
                await db.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
            await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

    asyncio.run(_run())
