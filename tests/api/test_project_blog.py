import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from src.workers.api.main import app

    return TestClient(app)


def test_generate_project_blog_route_registered_requires_auth(client):
    response = client.post(
        "/api/v1/projects/proj-1/blog/generate",
        json={"options": {}},
    )
    assert response.status_code in {401, 403}


def test_get_project_blog_route_registered_requires_auth(client):
    response = client.get("/api/v1/projects/proj-1/blog")
    assert response.status_code in {401, 403}


@pytest.mark.asyncio
async def test_generate_project_blog_includes_project_id_in_job_payload(monkeypatch):
    """Ensure generate_project_blog wires project_id through to job payload and enqueue call.

    We call the route function directly with a fake user dict and monkeypatched
    DB/helpers, bypassing FastAPI auth. This is a focused wiring test rather
    than an end-to-end auth test.
    """
    from src.workers.api import protected as protected_module
    from src.workers.api.models import ProjectGenerateBlogRequest, GenerateBlogOptions

    captured_job_args = {}
    captured_enqueue_payload = {}

    async def fake_get_project(db, project_id, user_id):  # type: ignore[unused-argument]
        return {
            "project_id": project_id,
            "document_id": "doc-1",
            "user_id": user_id,
            "youtube_url": "https://www.youtube.com/watch?v=dummy",
            "status": "embedded",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
        }

    async def fake_get_document(db, document_id, user_id=None):  # type: ignore[unused-argument]
        return {
            "document_id": document_id,
            "user_id": user_id,
            "raw_text": "some transcript text",
        }

    async def fake_get_user_prefs(db, user_id):  # type: ignore[unused-argument]
        return {}

    def fake_ensure_services():  # type: ignore[return-type]
        class FakeDB:
            ...

        class FakeQueue:
            ...

        return FakeDB(), FakeQueue()

    async def fake_create_job_extended(db, job_id, user_id, job_type, document_id, payload):  # type: ignore[unused-argument]
        captured_job_args.update(
            {
                "job_id": job_id,
                "user_id": user_id,
                "job_type": job_type,
                "document_id": document_id,
                "payload": payload,
            }
        )
        return {"job_id": job_id, "progress": "{}"}

    async def fake_enqueue_job_with_guard(queue, job_id, user_id, payload, allow_inline_fallback=False):  # type: ignore[unused-argument]
        captured_enqueue_payload.update(payload)
        return True, None, False

    # Force non-inline path so we exercise the enqueue wiring.
    monkeypatch.setattr(protected_module.settings, "use_inline_queue", False)
    monkeypatch.setattr(protected_module, "ensure_services", fake_ensure_services)
    monkeypatch.setattr(protected_module, "get_project", fake_get_project)
    monkeypatch.setattr(protected_module, "get_document", fake_get_document)
    monkeypatch.setattr(protected_module, "get_user_preferences", fake_get_user_prefs)
    monkeypatch.setattr(protected_module, "create_job_extended", fake_create_job_extended)
    monkeypatch.setattr(protected_module, "enqueue_job_with_guard", fake_enqueue_job_with_guard)

    req = ProjectGenerateBlogRequest(options=GenerateBlogOptions())
    user = {"user_id": "user-1"}

    resp = await protected_module.generate_project_blog("proj-1", req, user=user)

    assert resp.job_id == captured_job_args["job_id"]
    assert captured_job_args["payload"]["project_id"] == "proj-1"
    assert captured_enqueue_payload["project_id"] == "proj-1"


@pytest.mark.asyncio
async def test_get_project_blog_loads_latest_version(monkeypatch):
    """Ensure get_project_blog looks up the document's latest version and maps it into ProjectBlog."""
    from src.workers.api import protected as protected_module

    class FakeDB:
        ...

    async def fake_get_project(db, project_id, user_id):  # type: ignore[unused-argument]
        return {"project_id": project_id, "document_id": "doc-1", "user_id": user_id, "status": "blog_generated"}

    async def fake_get_document(db, document_id, user_id=None):  # type: ignore[unused-argument]
        return {
            "document_id": document_id,
            "user_id": user_id,
            "latest_version_id": "v1",
        }

    async def fake_get_document_version(db, document_id, version_id, user_id):  # type: ignore[unused-argument]
        return {
            "version_id": version_id,
            "document_id": document_id,
            "version": 1,
            "content_format": "mdx",
            "frontmatter": "{}",
            "body_mdx": "# Test",
            "body_html": "<h1>Test</h1>",
            "outline": "[]",
            "chapters": "[]",
            "sections": "[]",
            "assets": "{}",
            "created_at": "2025-01-01T00:00:00Z",
        }

    def fake_ensure_db():  # type: ignore[return-type]
        return FakeDB()

    monkeypatch.setattr(protected_module, "ensure_db", fake_ensure_db)
    monkeypatch.setattr(protected_module, "get_project", fake_get_project)
    monkeypatch.setattr(protected_module, "get_document", fake_get_document)
    monkeypatch.setattr(protected_module, "get_document_version", fake_get_document_version)

    user = {"user_id": "user-1"}
    blog = await protected_module.get_project_blog("proj-1", user=user)

    assert blog.project_id == "proj-1"
    assert blog.document_id == "doc-1"
    assert blog.version_id == "v1"
    assert blog.status == "blog_generated"
    assert blog.body_mdx == "# Test"
