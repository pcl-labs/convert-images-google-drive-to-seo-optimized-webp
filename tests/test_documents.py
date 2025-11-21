import uuid
from src.workers.api.database import create_document, get_document
import asyncio
import pytest


def test_create_and_get_document(isolated_db):
    db = isolated_db
    async def _run():
        try:
            document_id = str(uuid.uuid4())
            user_id = str(uuid.uuid4())
            created = await create_document(
                db,
                document_id=document_id,
                user_id=user_id,
                source_type="text",
                source_ref=None,
                raw_text="hello world",
                metadata={"title": "Test"},
            )
            assert created is not None
            assert created.get("document_id") == document_id

            fetched = await get_document(db, document_id, user_id=user_id)
            assert fetched is not None
            assert fetched.get("user_id") == user_id
            assert fetched.get("source_type") == "text"
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                maybe_coro = close()
                if hasattr(maybe_coro, "__await__"):
                    await maybe_coro
    asyncio.run(_run())
