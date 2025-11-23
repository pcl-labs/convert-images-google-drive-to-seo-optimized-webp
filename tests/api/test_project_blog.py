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

    async def fake_get_project(_db, project_id, user_id):
        return {
            "project_id": project_id,
            "document_id": "doc-1",
            "user_id": user_id,
            "youtube_url": "https://www.youtube.com/watch?v=dummy",
            "status": "embedded",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
        }

    async def fake_get_document(_db, document_id, user_id=None):
        return {
            "document_id": document_id,
            "user_id": user_id,
            "raw_text": "some transcript text",
        }

    async def fake_get_user_prefs(_db, _user_id):
        return {}

    class FakeDB:
        ...

    def fake_ensure_services():  # type: ignore[return-type]
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
    def fake_ensure_db():  # type: ignore[return-type]
        return FakeDB()

    monkeypatch.setattr(protected_module, "ensure_db", fake_ensure_db)
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

    async def fake_get_document_version(_db, document_id, version_id, _user_id):
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


def test_project_blog_new_routes_registered_require_auth(client):
    # Sections list
    resp = client.get("/api/v1/projects/proj-1/blog/sections")
    assert resp.status_code in {401, 403}
    # Sections patch
    resp = client.post(
        "/api/v1/projects/proj-1/blog/sections/patch",
        json={"section_id": "sec-0", "instructions": "Tighten intro"},
    )
    assert resp.status_code in {401, 403}
    # Versions list
    resp = client.get("/api/v1/projects/proj-1/blog/versions")
    assert resp.status_code in {401, 403}
    # Version detail
    resp = client.get("/api/v1/projects/proj-1/blog/versions/v1")
    assert resp.status_code in {401, 403}
    # Diff
    resp = client.get(
        "/api/v1/projects/proj-1/blog/diff",
        params={"from_version_id": "v1", "to_version_id": "v2"},
    )
    assert resp.status_code in {401, 403}
    # Revert
    resp = client.post("/api/v1/projects/proj-1/blog/versions/v1/revert")
    assert resp.status_code in {401, 403}
    # Export
    resp = client.get("/api/v1/projects/proj-1/blog/export")
    assert resp.status_code in {401, 403}


def test_get_project_activity_requires_auth(client):
    """Ensure project activity API is registered and protected by auth."""
    resp = client.get("/api/v1/projects/proj-1/activity")
    assert resp.status_code in {401, 403}


@pytest.mark.asyncio
async def test_get_project_activity_404_for_missing_project(monkeypatch):
    """get_project_activity should return 404 when project is not found for user."""
    from src.workers.api import protected as protected_module

    class FakeDB:
        ...

    async def fake_get_project(_db, _project_id, _user_id):  # type: ignore[unused-argument]
        return None

    def fake_ensure_db():  # type: ignore[return-type]
        return FakeDB()

    monkeypatch.setattr(protected_module, "ensure_db", fake_ensure_db)
    monkeypatch.setattr(protected_module, "get_project", fake_get_project)

    user = {"user_id": "user-1"}

    with pytest.raises(protected_module.HTTPException) as excinfo:
        await protected_module.get_project_activity("proj-missing", user=user)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_get_project_activity_returns_normalized_items(monkeypatch):
    """get_project_activity should return normalized activity items from list_project_activity."""
    from src.workers.api import protected as protected_module

    class FakeDB:
        ...

    async def fake_get_project(_db, project_id, user_id):  # type: ignore[unused-argument]
        return {"project_id": project_id, "user_id": user_id, "document_id": "doc-1"}

    async def fake_list_project_activity(_db, project_id, user_id, limit=30):  # type: ignore[unused-argument]
        return [
            {
                "id": "job:job-1",
                "kind": "job",
                "created_at": "2025-01-01T00:00:00Z",
                "status": "completed",
                "label": "Generate blog",
                "description": "Job completed",
                "job_id": "job-1",
                "event_type": None,
            }
        ]

    def fake_ensure_db():  # type: ignore[return-type]
        return FakeDB()

    monkeypatch.setattr(protected_module, "ensure_db", fake_ensure_db)
    monkeypatch.setattr(protected_module, "get_project", fake_get_project)
    monkeypatch.setattr(protected_module, "list_project_activity", fake_list_project_activity)

    user = {"user_id": "user-1"}

    payload = await protected_module.get_project_activity("proj-1", user=user)

    assert payload.project_id == "proj-1"
    assert isinstance(payload.items, list)
    assert payload.items[0]["id"] == "job:job-1"
    assert payload.items[0]["label"] == "Generate blog"


@pytest.mark.asyncio
async def test_patch_project_blog_section_creates_new_version(monkeypatch):
    from src.workers.api import protected as protected_module

    class FakeDB:
        ...

    project_row = {"project_id": "proj-1", "document_id": "doc-1", "user_id": "user-1"}
    base_version = {
        "version_id": "v1",
        "document_id": "doc-1",
        "user_id": "user-1",
        "content_format": "mdx",
        "frontmatter": "{}",
        "body_mdx": "# Title\n\n## Section 1\n\nOld body",
        "body_html": "<h1>Title</h1>",
        "outline": "[]",
        "chapters": "[]",
        "sections": "[{\"section_id\": \"sec-0\", \"index\": 0, \"title\": \"Section 1\", \"summary\": \"Old summary\"}]",
        "assets": "{}",
        "created_at": "2025-01-01T00:00:00Z",
    }

    async def fake_get_project(db, project_id, user_id):  # type: ignore[unused-argument]
        return project_row

    async def fake_get_document(db, document_id, user_id=None):  # type: ignore[unused-argument]
        return {
            "document_id": document_id,
            "user_id": user_id,
            "latest_version_id": "v2",
        }

    async def fake_get_latest_version_for_project(_db, _project, _user_id):
        return base_version

    async def fake_list_transcript_chunks(_db, _project_id, _user_id):
        return []

    async def fake_embed_texts(_texts):
        return [[0.1, 0.2, 0.3]]

    async def fake_query_project_chunks(**_kwargs):
        return []

    created_versions = {}

    async def fake_create_document_version(*_args, **kwargs):
        document_id = kwargs.get("document_id")
        body_mdx = kwargs.get("body_mdx")
        sections = kwargs.get("sections")
        created_versions["body_mdx"] = body_mdx
        created_versions["sections"] = sections
        return {
            "version_id": "v2",
            "document_id": document_id,
            "version": 2,
            "created_at": "2025-01-02T00:00:00Z",
            "body_mdx": body_mdx,
        }

    async def fake_update_latest(_db, _document_id, expected_version_id, new_version_id):
        created_versions["latest_version_updates"] = {
            "expected": expected_version_id,
            "new": new_version_id,
        }
        return True

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self._text = text

        def raise_for_status(self) -> None:  # pragma: no cover - trivial
            return None

        def json(self):  # pragma: no cover - trivial
            return {
                "choices": [
                    {
                        "message": {
                            "content": "New section body from AI",
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            ...

        async def post(self, *_args, **_kwargs):
            return FakeResponse("ok")

    def fake_ensure_db():  # type: ignore[return-type]
        return FakeDB()

    monkeypatch.setattr(protected_module, "ensure_db", fake_ensure_db)
    monkeypatch.setattr(protected_module, "get_project", fake_get_project)
    monkeypatch.setattr(protected_module, "get_latest_version_for_project", fake_get_latest_version_for_project)
    monkeypatch.setattr(protected_module, "list_transcript_chunks", fake_list_transcript_chunks)
    monkeypatch.setattr(protected_module, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(protected_module, "query_project_chunks", fake_query_project_chunks)
    monkeypatch.setattr(protected_module, "create_document_version", fake_create_document_version)
    monkeypatch.setattr(protected_module, "update_document_latest_version_if_match", fake_update_latest)
    monkeypatch.setattr(protected_module, "AsyncSimpleClient", FakeClient)

    user = {"user_id": "user-1"}
    req = protected_module.PatchSectionRequest(section_id="sec-0", instructions="Rewrite section")

    resp = await protected_module.patch_project_blog_section("proj-1", req, user=user)

    assert resp.version_id == "v2"
    assert resp.section.section_id == "sec-0"
    assert resp.section.body_mdx == "New section body from AI"
    assert created_versions["latest_version_updates"]["new"] == "v2"


@pytest.mark.asyncio
async def test_diff_and_revert_project_blog_versions(monkeypatch):
    from src.workers.api import protected as protected_module

    class FakeDB:
        ...

    project_row = {"project_id": "proj-1", "document_id": "doc-1", "user_id": "user-1"}

    base_v1 = {
        "version_id": "v1",
        "document_id": "doc-1",
        "user_id": "user-1",
        "version": 1,
        "content_format": "mdx",
        "frontmatter": "{}",
        "body_mdx": "# Title\n\n## Section 1\n\nOld body",
        "body_html": "<h1>Title</h1>",
        "outline": "[]",
        "chapters": "[]",
        "sections": "[{\"section_id\": \"sec-0\", \"index\": 0, \"title\": \"Section 1\", \"summary\": \"Old body\"}]",
        "assets": "{}",
        "created_at": "2025-01-01T00:00:00Z",
    }
    base_v2 = dict(base_v1)
    base_v2.update(
        {
            "version_id": "v2",
            "version": 2,
            "body_mdx": "# Title\n\n## Section 1\n\nNew body",
            "sections": "[{\"section_id\": \"sec-0\", \"index\": 0, \"title\": \"Section 1\", \"summary\": \"New body\"}]",
        }
    )

    async def fake_get_project(db, project_id, user_id):  # type: ignore[unused-argument]
        return project_row

    async def fake_get_document(db, document_id, user_id=None):  # type: ignore[unused-argument]
        return {
            "document_id": document_id,
            "user_id": user_id,
            "latest_version_id": "v2",
        }

    async def fake_get_document_version(db, document_id, version_id, user_id):  # type: ignore[unused-argument]
        if version_id == "v1":
            return base_v1
        if version_id == "v2":
            return base_v2
        return None

    created_revert = {}

    async def fake_create_document_version(db, document_id, user_id, content_format, frontmatter, body_mdx, body_html, outline, chapters, sections, assets):  # type: ignore[unused-argument]
        created_revert["body_mdx"] = body_mdx
        return {
            "version_id": "v3",
            "document_id": document_id,
            "version": 3,
            "created_at": "2025-01-03T00:00:00Z",
            "body_mdx": body_mdx,
        }

    async def fake_update_latest(db, document_id, expected_version_id, new_version_id):  # type: ignore[unused-argument]
        created_revert["latest_version_updates"] = {
            "expected": expected_version_id,
            "new": new_version_id,
        }
        return True

    def fake_ensure_db():  # type: ignore[return-type]
        return FakeDB()

    monkeypatch.setattr(protected_module, "ensure_db", fake_ensure_db)
    monkeypatch.setattr(protected_module, "get_project", fake_get_project)
    monkeypatch.setattr(protected_module, "get_document", fake_get_document)
    monkeypatch.setattr(protected_module, "get_document_version", fake_get_document_version)
    monkeypatch.setattr(protected_module, "create_document_version", fake_create_document_version)
    monkeypatch.setattr(protected_module, "update_document_latest_version_if_match", fake_update_latest)

    user = {"user_id": "user-1"}

    diff = await protected_module.diff_project_blog_versions("proj-1", "v1", "v2", user=user)
    assert diff.project_id == "proj-1"
    assert diff.from_version_id == "v1"
    assert diff.to_version_id == "v2"
    assert "sec-0" in diff.changed_sections

    reverted = await protected_module.revert_project_blog_version("proj-1", "v1", user=user)
    assert reverted.version_id == "v3"
    assert created_revert["latest_version_updates"]["new"] == "v3"
