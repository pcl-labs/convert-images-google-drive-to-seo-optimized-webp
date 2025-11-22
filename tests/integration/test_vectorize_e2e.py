import os
import uuid

import pytest

from api.config import settings
from src.workers.core.vectorize_client import store_embeddings, query_project_chunks


pytestmark = pytest.mark.asyncio


@pytest.mark.skipif(
    os.getenv("VECTORIZE_E2E") != "1",
    reason="Set VECTORIZE_E2E=1 to run live Vectorize roundtrip test",
)
async def test_vectorize_roundtrip():
    """Live roundtrip test against Cloudflare Vectorize.

    This is intentionally opt-in and should only be run when you have
    configured CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN with
    Vectorize permissions, and created the quill-transcripts index.
    """
    if not settings.cloudflare_account_id or not os.getenv("CLOUDFLARE_API_TOKEN"):
        pytest.skip("Vectorize env not configured")

    project_id = f"test-proj-{uuid.uuid4()}"
    document_id = f"test-doc-{uuid.uuid4()}"

    # Simple toy vector; we only care about roundtrip, not semantics.
    vec = [0.1, 0.2, 0.3]
    vectors = [vec]
    metadatas = [
        {
            "project_id": project_id,
            "document_id": document_id,
            "chunk_index": 0,
        }
    ]

    try:
        stored = await store_embeddings(vectors=vectors, metadatas=metadatas)
        assert stored == 1

        hits = await query_project_chunks(
            project_id=project_id,
            query_vector=vec,
            limit=1,
        )
    except RuntimeError as exc:
        # Outside of a Workers runtime, simple_http raises a clear RuntimeError
        # about fetch() not being available. In that case, skip instead of
        # failing this e2e test.
        if "fetch API not available" in str(exc):
            pytest.skip("Vectorize e2e test requires Cloudflare Workers fetch API")
        raise

    assert hits, "Expected at least one Vectorize match"

    meta = (hits[0].get("metadata") or {})
    assert meta.get("project_id") == project_id
    assert meta.get("document_id") == document_id
