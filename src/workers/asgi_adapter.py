"""
Utilities to route Cloudflare Worker requests into an ASGI application.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Tuple
from urllib.parse import urlsplit

from js import Response  # type: ignore
from pyodide.ffi import to_bytes, to_js, to_py  # type: ignore

HeadersList = List[Tuple[bytes, bytes]]
ASGIReceiveCallable = Callable[[], Awaitable[Dict[str, Any]]]
ASGISendCallable = Callable[[Dict[str, Any]], Awaitable[None]]


async def _extract_headers(request: Any) -> HeadersList:
    headers: HeadersList = []
    entries = to_py(request.headers.entries())
    for key, value in entries:
        headers.append((key.lower().encode("latin-1"), str(value).encode("latin-1")))
    return headers


async def _extract_body(request: Any) -> bytes:
    array_buffer = await request.arrayBuffer()
    return to_bytes(array_buffer)


async def handle_worker_request(app, request, env, ctx):
    """
    Adapt the Workers runtime (request/env/ctx) into the ASGI interface FastAPI expects.
    """
    url = str(request.url)
    split = urlsplit(url)
    path = split.path or ""
    query = split.query or ""
    headers = await _extract_headers(request)
    cf_client_ip = None
    for header_key, header_value in headers:
        if header_key == b"cf-connecting-ip":
            cf_client_ip = header_value.decode("latin-1")
            break
    client_host = cf_client_ip or getattr(getattr(request, "client", None), "host", "")
    client_tuple = (client_host, 0)
    scope: Dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "method": request.method,
        "scheme": split.scheme or "https",
        "root_path": "",
        "path": path or "/",
        "raw_path": path.encode("ascii", "ignore"),
        "query_string": query.encode("ascii", "ignore"),
        "client": client_tuple,
        "server": (split.hostname or "", split.port or (443 if split.scheme == "https" else 80)),
        "headers": headers,
    }

    body = await _extract_body(request)
    body_sent = False

    async def receive() -> Dict[str, Any]:
        nonlocal body_sent
        if body_sent:
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}
        body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    response_body = bytearray()
    status_code = 500
    response_headers: HeadersList = []

    async def send(message: Dict[str, Any]) -> None:
        nonlocal status_code, response_headers
        message_type = message["type"]
        if message_type == "http.response.start":
            status_code = message["status"]
            response_headers = message.get("headers", [])
        elif message_type == "http.response.body":
            response_body.extend(message.get("body", b""))
        else:  # pragma: no cover - ASGI extensions
            pass

    await app(scope, receive, send)

    py_headers = [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in response_headers
    ]
    response_init = to_js({"status": status_code, "headers": py_headers})
    return Response.new(bytes(response_body), response_init)


__all__ = ["handle_worker_request"]
