"""
Basic tests for new ingestion endpoints.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from src.workers.api.main import app
    return TestClient(app)


def test_ingest_text_requires_auth(client):
    resp = client.post("/ingest/text", json={"text": "hello"})
    assert resp.status_code in [401, 403]


def test_ingest_youtube_requires_auth(client):
    resp = client.post("/ingest/youtube", json={"url": "https://youtu.be/abc12345678"})
    assert resp.status_code in [401, 403]
