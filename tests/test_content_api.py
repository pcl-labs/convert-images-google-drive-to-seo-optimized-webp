import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from src.workers.api.content import (
    ContentMode,
    ContentFormat,
    _resolve_format,
    _build_sync_response,
    _build_job_links,
)
from src.workers.api.models import JobStatus, JobStatusEnum, JobProgress


def test_resolve_format_defaults():
    assert _resolve_format(ContentMode.structured, None) == ContentFormat.json
    assert _resolve_format(ContentMode.markdown, None) == ContentFormat.mdx


def test_build_sync_response_structured():
    job_row = {
        "output": json.dumps({
            "version_id": "ver-1",
            "body": {"mdx": "# Title", "html": "<h1>Title</h1>"},
        })
    }
    response = _build_sync_response(job_row, ContentMode.structured, ContentFormat.json, "doc-1")
    assert response.document_id == "doc-1"
    assert response.version_id == "ver-1"
    assert response.content["body"]["mdx"] == "# Title"


def test_build_sync_response_markdown_html():
    job_row = {
        "output": json.dumps({
            "version_id": "ver-1",
            "body": {"mdx": "# Title", "html": "<p>hello</p>"},
        })
    }
    response = _build_sync_response(job_row, ContentMode.markdown, ContentFormat.html, "doc-1")
    assert response.body == "<p>hello</p>"


def test_build_sync_response_missing_body():
    job_row = {
        "output": json.dumps({
            "version_id": "ver-1",
            "body": {"mdx": None, "html": None},
        })
    }
    with pytest.raises(HTTPException):
        _build_sync_response(job_row, ContentMode.markdown, ContentFormat.mdx, "doc-1")


def test_build_job_links_includes_version_links():
    now = datetime.now(timezone.utc)
    job = JobStatus(
        job_id="job-123",
        user_id="user-1",
        status=JobStatusEnum.COMPLETED,
        progress=JobProgress(stage="done"),
        created_at=now,
        completed_at=now,
        error=None,
        job_type="generate_blog",
        document_id="doc-123",
        output={"version_id": "ver-9"},
    )
    links = _build_job_links(job)
    assert links["document"] == "/v1/documents/doc-123"
    assert links["latest_version"] == "/v1/documents/doc-123/versions/latest"
    assert links["version"] == "/v1/documents/doc-123/versions/ver-9"
