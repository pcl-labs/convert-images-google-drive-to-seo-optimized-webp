import uuid
from api.database import Database, create_document, get_document


def test_create_and_get_document():
    db = Database()
    document_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    # Create
    created = None
    import asyncio
    async def _run():
        nonlocal created
        created = await create_document(
            db,
            document_id=document_id,
            user_id=user_id,
            source_type="text",
            source_ref=None,
            raw_text="hello world",
            metadata={"title": "Test"},
        )
    asyncio.run(_run())
    assert created is not None
    assert created.get("document_id") == document_id
    # Get
    fetched = None
    async def _run_get():
        nonlocal fetched
        fetched = await get_document(db, document_id, user_id=user_id)
    asyncio.run(_run_get())
    assert fetched is not None
    assert fetched.get("user_id") == user_id
    assert fetched.get("source_type") == "text"
