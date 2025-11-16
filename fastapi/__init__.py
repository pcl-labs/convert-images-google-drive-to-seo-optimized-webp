"""Lightweight FastAPI compatibility shims for offline testing.

This stub implements the minimal surface that our unit tests import
without pulling in the full FastAPI dependency tree. It purposely keeps
behaviour simple – the goal is to provide the same symbols so modules
can be imported even when installing FastAPI is not possible in the CI
sandbox.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Coroutine, Iterable, Optional


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: Any | None = None, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}


class _StatusCodes:
    HTTP_200_OK = 200
    HTTP_204_NO_CONTENT = 204
    HTTP_302_FOUND = 302
    HTTP_303_SEE_OTHER = 303
    HTTP_400_BAD_REQUEST = 400
    HTTP_401_UNAUTHORIZED = 401
    HTTP_403_FORBIDDEN = 403
    HTTP_404_NOT_FOUND = 404
    HTTP_409_CONFLICT = 409
    HTTP_422_UNPROCESSABLE_ENTITY = 422
    HTTP_429_TOO_MANY_REQUESTS = 429
    HTTP_500_INTERNAL_SERVER_ERROR = 500
    HTTP_501_NOT_IMPLEMENTED = 501
    HTTP_502_BAD_GATEWAY = 502


status = _StatusCodes()


class Request:
    def __init__(self, scope: Optional[dict[str, Any]] = None):
        self.scope = scope or {}
        self.state = SimpleNamespace()
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, Any] = {}
        self.path_params: dict[str, Any] = {}


class Response:
    def __init__(self, content: Any = None, status_code: int = status.HTTP_200_OK, headers: Optional[dict[str, str]] = None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


class APIRouter:
    def __init__(self, *args: Any, **kwargs: Any):
        self.routes: list[tuple[str, Callable[..., Any], tuple[str, ...]]] = []

    def add_api_route(self, path: str, endpoint: Callable[..., Any], methods: Iterable[str] | None = None, **_: Any):
        method_tuple = tuple(methods or ())
        self.routes.append((path, endpoint, method_tuple))
        return endpoint

    def _register(self, method: str, path: str, **kwargs: Any):
        def decorator(func: Callable[..., Any]):
            self.add_api_route(path, func, methods=[method], **kwargs)
            return func

        return decorator

    def get(self, path: str, **kwargs: Any):
        return self._register("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any):
        return self._register("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any):
        return self._register("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any):
        return self._register("DELETE", path, **kwargs)


class FastAPI(APIRouter):
    def __init__(self, *args: Any, **kwargs: Any):  # pragma: no cover - compatibility only
        super().__init__(*args, **kwargs)
        self.middleware_stack = []

    def add_middleware(self, middleware_class: type, **options: Any):
        self.middleware_stack.append((middleware_class, options))

    def mount(self, path: str, app: Any, **kwargs: Any):
        self.routes.append((path, app, ("MOUNT",)))


class Depends:
    def __init__(self, dependency: Callable[..., Any] | None = None):
        self.dependency = dependency


def Form(default: Any = None, **_: Any):
    return default


def Query(default: Any = None, **_: Any):
    return default


# Re-export response helpers
from .responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse  # noqa: E402
from .responses import Response as Response  # type: ignore  # noqa: E402

# Submodules
from . import responses, staticfiles, testclient  # noqa: F401, E402

__all__ = [
    "APIRouter",
    "Depends",
    "FastAPI",
    "Form",
    "HTTPException",
    "HTMLResponse",
    "JSONResponse",
    "PlainTextResponse",
    "Query",
    "RedirectResponse",
    "Request",
    "Response",
    "responses",
    "staticfiles",
    "status",
    "testclient",
]
