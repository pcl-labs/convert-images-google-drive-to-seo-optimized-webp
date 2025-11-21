import asyncio
import json
import uuid

from src.workers.api.database import (
    Database,
    create_document,
    create_job_extended,
    get_job,
    get_document,
    create_user,
    update_user_preferences,
)
from src.workers.api.models import JobType
from src.workers.consumer import process_generate_blog_job


def test_process_generate_blog_job_creates_output(isolated_db):
    async def _run():
        db = isolated_db
        user_id = "pipeline-user"
        document_id = str(uuid.uuid4())
        from tests.conftest import create_test_user
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
            assert output["options"]["instructions"] == instructions
            assert isinstance(output.get("plan"), dict)
            assert output["plan"].get("content_type") == "generic_blog"
            assert output["plan"].get("instructions") == instructions

            stored_doc = await get_document(db, document_id, user_id=user_id)
            assert stored_doc is not None
            metadata = stored_doc.get("metadata")
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            assert metadata.get("latest_generation", {}).get("job_id") == job_id
            latest_gen = metadata.get("latest_generation", {})
            assert latest_gen.get("model")
            assert latest_gen.get("tone") == "playful"
            assert latest_gen.get("content_type") == "generic_blog"
            assert latest_gen.get("instructions") == instructions
            outline_snapshot = metadata.get("latest_outline")
            assert isinstance(outline_snapshot, list)
            assert outline_snapshot
            assert outline_snapshot[0].get("slot") == "intro"
            plan_snapshot = metadata.get("content_plan")
            assert isinstance(plan_snapshot, dict)
            assert plan_snapshot.get("content_type") == "generic_blog"
            assert plan_snapshot.get("instructions") == instructions
            assert plan_snapshot.get("schema_version") == 1
            assert plan_snapshot.get("sections")
            assert plan_snapshot["sections"][0]["title"]
            assert stored_doc.get("latest_version_id")

            versions = await db.execute_all("SELECT * FROM document_versions WHERE document_id = ?", (document_id,))
            assert versions
            version_row = dict(versions[0])
            assert version_row.get("body_mdx")
            assert version_row.get("content_format") == "mdx"
            assets = json.loads(version_row.get("assets") or "{}")
            assert assets.get("generator", {}).get("model")
        finally:
            # Cleanup: delete in FK-safe order
            await db.execute("DELETE FROM document_versions WHERE document_id = ?", (document_id,))
            await db.execute("DELETE FROM usage_events WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
            await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

    asyncio.run(_run())
