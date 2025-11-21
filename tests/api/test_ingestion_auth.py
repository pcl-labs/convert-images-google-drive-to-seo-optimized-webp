import uuid
import asyncio
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from tests.conftest import create_test_user
from src.workers.api.deps import set_queue_producer
from src.workers.api.cloudflare_queue import QueueProducer


@pytest.fixture
def authed_client(isolated_db):
    from src.workers.api.main import app
    from src.workers.api.auth import generate_jwt_token

    client = TestClient(app)

    # Create user in isolated SQLite DB
    user_id = f"test_{uuid.uuid4()}"
    email = f"{user_id}@example.com"
    
    # Use create_test_user helper which ensures unique provider IDs
    asyncio.run(create_test_user(isolated_db, user_id=user_id, email=email))
    
    # Set up a mock queue producer for ensure_services()
    mock_queue = MagicMock()
    mock_queue.send = AsyncMock(return_value=True)
    queue_producer = QueueProducer(queue=mock_queue)
    set_queue_producer(queue_producer)

    # Issue JWT and set as cookie (include email so middleware doesn't need DB lookup)
    token = generate_jwt_token(user_id=user_id, email=email)
    client.cookies.set("access_token", token)

    client.test_user_id = user_id
    return client


def test_ingest_text_authed(authed_client):
    resp = authed_client.post("/ingest/text", json={"text": "hello world", "title": "t"})
    assert resp.status_code in [200, 201]
    data = resp.json()
    assert data.get("job_type") == "ingest_text"
    assert data.get("document_id")
    assert data.get("job_id")


def test_ingest_youtube_authed(authed_client, monkeypatch):
    fake_service = object()
    monkeypatch.setattr("src.workers.api.protected.build_youtube_service_for_user", AsyncMock(return_value=fake_service))

    metadata_bundle = {
        "frontmatter": {"title": "Sample Video", "slug": "sample-video"},
        "metadata": {
            "title": "Sample Video",
            "description": "Demo",
            "duration_seconds": 120,
            "channel_title": "Channel",
            "channel_id": "chan123",
            "published_at": "2024-01-01T00:00:00Z",
            "thumbnails": {},
            "category_id": "24",
            "tags": ["demo"],
        },
    }

    async def _fake_fetch_async(service, video_id):
        assert service is fake_service
        assert video_id == "abc12345678"
        return metadata_bundle

    monkeypatch.setattr("src.workers.api.protected.fetch_video_metadata_async", _fake_fetch_async)

    async def _fake_ingest(db, job_id, user_id, document_id, video_id, metadata, frontmatter_payload, duration):
        return {
            "job_output": {
                "document_id": document_id,
                "youtube_video_id": video_id,
                "transcript": {"chars": 10, "duration_s": duration, "lang": "en"},
                "metadata": {"frontmatter": frontmatter_payload, "youtube": metadata},
            }
        }

    monkeypatch.setattr("src.workers.api.protected.ingest_youtube_document", _fake_ingest)

    # Use a simple valid-looking short URL pattern matched by regex
    resp = authed_client.post("/ingest/youtube", json={"url": "https://youtu.be/abc12345678"})
    assert resp.status_code in [200, 201]
    data = resp.json()
    assert data.get("job_type") == "ingest_youtube"
    assert data.get("document_id")
    assert data.get("job_id")
