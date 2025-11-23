import asyncio
import json
import uuid

from tests.conftest import create_test_user
from src.workers.api.database import (
    Database,
    create_document,
    create_job_extended,
    get_job,
    get_document,
    update_user_preferences,
    update_job_status,
    set_job_output,
)
from src.workers.api.models import JobType, JobStatusEnum, GenerateBlogRequest, GenerateBlogOptions
from src.workers.consumer import process_generate_blog_job
from src.workers.api import protected as protected_module


def test_start_generate_blog_job_runs_inline(monkeypatch, isolated_db):
    async def _run():
        db = isolated_db
        user_id = "inline-user"
        document_id = str(uuid.uuid4())
        await create_test_user(db, user_id=user_id, email="inline@example.com")
        await create_document(
            db,
            document_id=document_id,
            user_id=user_id,
            source_type="text",
            source_ref=None,
            raw_text="Inline test content for blog generation.",
            metadata={"title": "Inline Test"},
        )

        async def fake_process_generate_blog_job(db, job_id, user_id, document_id, options=None, pipeline_job_id=None):
            await update_job_status(db, job_id, JobStatusEnum.COMPLETED.value, progress={"stage": "completed"})
            await set_job_output(
                db,
                job_id,
                {
                    "frontmatter": {"title": "Inline Draft"},
                    "body": {"mdx": "# Inline Draft", "html": "<h1>Inline Draft</h1>"},
                },
            )

        monkeypatch.setattr(protected_module.settings, "use_inline_queue", True)
        monkeypatch.setattr(protected_module, "process_generate_blog_job", fake_process_generate_blog_job)

        request = GenerateBlogRequest(document_id=document_id, options=GenerateBlogOptions())
        status = await protected_module.start_generate_blog_job(db, None, user_id, request)

        assert status.status == JobStatusEnum.COMPLETED
        job_row = await get_job(db, status.job_id, user_id)
        assert job_row is not None
        assert job_row["status"] == "completed"

    asyncio.run(_run())


def test_process_generate_blog_job_creates_output(monkeypatch, isolated_db):
    async def _run():
        db = isolated_db
        user_id = "pipeline-user"
        document_id = str(uuid.uuid4())
        await create_test_user(db, user_id=user_id, email="pipeline@example.com")
        await update_user_preferences(
            db,
            user_id,
            {
                "ai": {
                    "tone": "playful",
                    "model": "gpt-4o-mini",
                    "max_sections": 3,
                    "target_chapters": 3,
                    "include_images": True,
                }
            },
        )
        await create_document(
            db,
            document_id=document_id,
            user_id=user_id,
            source_type="text",
            source_ref=None,
            raw_text=(
                "This is a sample transcript describing how to grow an audience online. "
                "It contains multiple paragraphs and insights. Use it to generate chapters."
            ),
            metadata={"title": "Test Document"},
        )
        job_id = str(uuid.uuid4())
        await create_job_extended(
            db,
            job_id,
            user_id,
            job_type=JobType.GENERATE_BLOG.value,
            document_id=document_id,
        )

        instructions = "Focus on actionable tips for creators."

        # Avoid hitting real Cloudflare AI Gateway during tests by mocking
        # the compose_blog_from_text helper used inside the consumer.
        from src.workers import consumer as consumer_module

        async def fake_compose_blog_from_text(*_args, **_kwargs):
            return {
                "markdown": "# Inline Draft\n\nBody",
                "meta": {
                    "tone": "playful",
                    "sections": 3,
                    "word_count": 100,
                    "engine": "test",
                    "model": "gpt-4o-mini",
                    "temperature": 0.6,
                },
            }

        monkeypatch.setattr(consumer_module, "compose_blog_from_text", fake_compose_blog_from_text)
        await process_generate_blog_job(
            db,
            job_id,
            user_id,
            document_id,
            {
                "tone": "playful",
                "max_sections": 3,
                "target_chapters": 3,
                "include_images": True,
                "instructions": instructions,
            },
        )

        try:
            job_row = await get_job(db, job_id, user_id)
            assert job_row is not None
            assert job_row["status"] == "completed"
            assert job_row.get("output")
            output = json.loads(job_row["output"])
            assert output["frontmatter"]["title"]
            assert output["body"]["mdx"]
            assert output["body"]["html"]
            assert output["sections"]
            assert output["options"]["tone"] == "playful"
            assert output["options"]["model"]
            assert output["options"]["content_type"] == "generic_blog"
            assert output["options"]["schema_type"] == "https://schema.org/BlogPosting"
            assert output["options"]["instructions"] == instructions

            stored_doc = await get_document(db, document_id, user_id=user_id)
            assert stored_doc is not None
            metadata = stored_doc.get("metadata")
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            latest_gen = metadata.get("latest_generation", {})
            assert latest_gen.get("model")
            assert latest_gen.get("tone") == "playful"
            assert latest_gen.get("content_type") == "generic_blog"
            assert latest_gen.get("schema_type") == "https://schema.org/BlogPosting"
            assert latest_gen.get("instructions") == instructions
            outline_snapshot = metadata.get("latest_outline")
            assert isinstance(outline_snapshot, list)
            assert outline_snapshot
            assert outline_snapshot[0].get("slot") == "intro"
            plan_snapshot = metadata.get("content_plan")
            assert isinstance(plan_snapshot, dict)
            assert plan_snapshot.get("schema_type") == "https://schema.org/BlogPosting"
            assert plan_snapshot.get("structured") is not None or plan_snapshot.get("content_type")
            assert stored_doc.get("latest_version_id")

            versions = await db.execute_all("SELECT * FROM document_versions WHERE document_id = ?", (document_id,))
            assert versions
            version_row = dict(versions[0])
            assert version_row.get("body_mdx")
            assert version_row.get("content_format") == "mdx"
            assets = json.loads(version_row.get("assets") or "{}")
            assert assets.get("generator", {}).get("model")
            if assets.get("schema"):
                assert assets["schema"].get("type") == "https://schema.org/BlogPosting"
        finally:
            # Cleanup: delete in FK-safe order (temp DB makes this less critical, but keep for explicit cleanup)
            await db.execute("DELETE FROM document_versions WHERE document_id = ?", (document_id,))
            await db.execute("DELETE FROM usage_events WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
            # User deletion not needed - temp DB is cleaned up automatically

    asyncio.run(_run())
