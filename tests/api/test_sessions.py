"""Tests covering session persistence and notifications stream cursor behavior."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest


def _ensure_pydantic_stub() -> None:
    """Install a lightweight pydantic stub if the dependency is missing."""

    if importlib.util.find_spec("pydantic") is not None or "pydantic" in sys.modules:
        return

    stub = types.ModuleType("pydantic")

    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def model_dump(self, *_, **__):  # pragma: no cover - helper for compatibility
            return self.__dict__.copy()

    def Field(default=None, **_kwargs):  # noqa: D401
        return default

    def _passthrough_decorator(*_args, **_kwargs):  # noqa: D401
        def _decorator(func):
            return func

        return _decorator

    stub.BaseModel = BaseModel
    stub.Field = Field
    stub.field_validator = _passthrough_decorator
    stub.model_validator = _passthrough_decorator

    class ConfigDict(dict):
        pass

    stub.ConfigDict = ConfigDict
    stub.HttpUrl = str
    sys.modules["pydantic"] = stub


def _ensure_starlette_stub() -> None:
    """Install a minimal starlette.responses stub for StreamingResponse."""

    if importlib.util.find_spec("starlette") is not None:
        return
    if "starlette" in sys.modules and hasattr(sys.modules["starlette"], "responses"):
        return

    starlette_pkg = types.ModuleType("starlette")
    responses_module = types.ModuleType("starlette.responses")

    class StreamingResponse:  # pragma: no cover - simple async wrapper for tests
        def __init__(self, iterator, headers=None):
            self.body_iterator = iterator
            self.headers = headers or {}

    responses_module.StreamingResponse = StreamingResponse
    starlette_pkg.responses = responses_module
    sys.modules["starlette"] = starlette_pkg
    sys.modules["starlette.responses"] = responses_module


def _ensure_fastapi_stub() -> None:
    """Install a fastapi stub that provides HTTPException and status codes."""

    if importlib.util.find_spec("fastapi") is not None or "fastapi" in sys.modules:
        return

    fastapi_module = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str | None = None, headers: dict | None = None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}

    class _Status:
        HTTP_401_UNAUTHORIZED = 401
        HTTP_403_FORBIDDEN = 403
        HTTP_404_NOT_FOUND = 404
        HTTP_422_UNPROCESSABLE_ENTITY = 422
        HTTP_429_TOO_MANY_REQUESTS = 429
        HTTP_500_INTERNAL_SERVER_ERROR = 500

    fastapi_module.HTTPException = HTTPException
    fastapi_module.status = _Status()

    def _identity(value=None):  # noqa: D401
        return value

    fastapi_module.Depends = _identity
    fastapi_module.Form = _identity
    fastapi_module.Request = object  # type: ignore[assignment]
    fastapi_module.Response = object  # type: ignore[assignment]
    sys.modules["fastapi"] = fastapi_module


_ensure_pydantic_stub()
_ensure_starlette_stub()
_ensure_fastapi_stub()

from src.workers.api.database import (  # noqa: E402  # pylint: disable=wrong-import-position
    Database,
    ensure_sessions_schema,
    create_user,
    create_user_session,
    get_user_session,
    touch_user_session,
    delete_user_session,
)
from src.workers.api import notifications_stream  # noqa: E402  # pylint: disable=wrong-import-position
from src.workers.api.notifications_stream import notifications_stream_response  # noqa: E402  # pylint: disable=wrong-import-position


@pytest.fixture()
def isolated_db(tmp_path):
    """Provide a sqlite-backed Database instance isolated to this test."""

    original_path = os.environ.get("LOCAL_SQLITE_PATH")
    db_path = tmp_path / "sessions-test.db"
    os.environ["LOCAL_SQLITE_PATH"] = str(db_path)
    db = Database()
    try:
        yield db
    finally:
        if original_path is None:
            os.environ.pop("LOCAL_SQLITE_PATH", None)
        else:
            os.environ["LOCAL_SQLITE_PATH"] = original_path
        try:
            if db_path.exists():
                db_path.unlink()
        except Exception:
            pass


def test_session_lifecycle_persists_metadata(isolated_db):
    async def _run():
        db = isolated_db
        await ensure_sessions_schema(db)
        user_id = "session-user"
        await create_user(db, user_id, email="session@example.com")

        expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
        session_id = "sess-123"
        await create_user_session(
            db,
            session_id,
            user_id,
            expires_at,
            ip_address="127.0.0.1",
            user_agent="pytest",
            extra={"provider": "github"},
        )

        stored = await get_user_session(db, session_id)
        assert stored is not None
        assert stored["user_id"] == user_id
        assert stored["ip_address"] == "127.0.0.1"
        assert stored["user_agent"] == "pytest"
        assert json.loads(stored["extra"]) == {"provider": "github"}

        await touch_user_session(
            db,
            session_id,
            last_notification_id="notif-1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=3),
        )
        updated = await get_user_session(db, session_id)
        assert updated is not None
        assert updated["last_notification_id"] == "notif-1"

        await delete_user_session(db, session_id)
        assert await get_user_session(db, session_id) is None

    asyncio.run(_run())


def test_notifications_stream_reuses_session_cursor(monkeypatch):
    async def _run():
        session: Dict[str, Any] = {"session_id": "sess-1", "last_notification_id": "cursor-1"}
        user = {"user_id": "user-1"}
        recorded_after_ids: List[Any] = []
        recorded_touch: Dict[str, Any] = {}

        async def fake_list_notifications(db, user_id, after_id=None, limit=None):  # noqa: D401
            recorded_after_ids.append(after_id)
            if len(recorded_after_ids) == 1:
                return [
                    {
                        "id": "notif-new",
                        "level": "info",
                        "text": "hello",
                        "created_at": "2024-01-01T00:00:00Z",
                    }
                ]
            return []

        async def fake_touch_user_session(db, session_id, **kwargs):  # noqa: D401
            recorded_touch["session_id"] = session_id
            recorded_touch["last_notification_id"] = kwargs.get("last_notification_id")

        async def immediate_sleep(_):
            return None

        monkeypatch.setattr(notifications_stream, "list_notifications", fake_list_notifications)
        monkeypatch.setattr(notifications_stream, "touch_user_session", fake_touch_user_session)
        monkeypatch.setattr(notifications_stream.asyncio, "sleep", immediate_sleep)

        class DummyRequest:
            def __init__(self):
                self.state = SimpleNamespace()
                self._checks = 0

            async def is_disconnected(self):  # noqa: D401
                self._checks += 1
                return self._checks > 2

        response = notifications_stream_response(DummyRequest(), db=object(), user=user, session=session)
        chunks: List[str] = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            data = chunk.decode() if isinstance(chunk, (bytes, bytearray)) else str(chunk)
            chunks.append(data)

        assert any("notification.created" in chunk for chunk in chunks)
        assert recorded_after_ids == ["cursor-1", "notif-new"]
        assert recorded_touch == {"session_id": "sess-1", "last_notification_id": "notif-new"}
        assert session["last_notification_id"] == "notif-new"

    asyncio.run(_run())
