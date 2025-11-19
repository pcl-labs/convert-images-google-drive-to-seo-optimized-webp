"""
Utilities to route Cloudflare Worker requests into an ASGI application.

This module adapts Cloudflare Worker HTTP requests to the ASGI interface
that FastAPI expects, allowing the same FastAPI app to run in both:
- Local development (via Uvicorn)
- Cloudflare Python Workers (via this adapter)

Known Limitations (Phase 1):
- Streaming responses are NOT supported. Endpoints that return StreamingResponse
  (e.g., Server-Sent Events like /api/stream and /api/pipeline_stream) will
  not work correctly in the Worker runtime. This will be addressed in a future phase.
- Only single-response bodies are handled. Multiple http.response.body messages
  with more_body=True are not supported.
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

    Args:
        app: The ASGI application (FastAPI app instance)
        request: Cloudflare Worker Request object
        env: Worker environment bindings (DB, queues, etc.)
        ctx: Worker execution context

    Returns:
        Cloudflare Worker Response object

    Note:
        This adapter only handles single-response bodies. Streaming responses
        (StreamingResponse, SSE) are not supported in Phase 1.
    """
    # Parse the request URL to extract path, query, scheme, etc.
    url = str(request.url)
    try:
        split = urlsplit(url)
    except Exception:
        # Fallback for malformed URLs - use defaults
        split = type('obj', (object,), {
            'path': '/',
            'query': '',
            'scheme': 'https',
            'hostname': '',
            'port': None
        })()
    
    path = split.path or ""
    query = split.query or ""
    
    # Try to get raw bytes if available, otherwise encode with UTF-8 surrogateescape
    # to preserve all bytes without loss (handles non-UTF-8 paths correctly)
    raw_path_bytes = getattr(request, "raw_path_bytes", None)
    if raw_path_bytes is None:
        raw_path_bytes = path.encode("utf-8", "surrogateescape")

    # Query string as bytes (empty string becomes empty bytes, not None)
    raw_query_bytes = getattr(request, "raw_query_bytes", None)
    if raw_query_bytes is None:
        raw_query_bytes = query.encode("utf-8", "surrogateescape")
    
    # Extract headers as list of (name: bytes, value: bytes) tuples
    # ASGI spec requires headers in this format, lowercase names
    headers = await _extract_headers(request)
    
    # Extract client IP from Cloudflare-specific header if available
    # Falls back to request.client if present, otherwise empty string
    cf_client_ip = None
    for header_key, header_value in headers:
        if header_key == b"cf-connecting-ip":
            cf_client_ip = header_value.decode("latin-1")
            break
    client = getattr(request, "client", None)
    client_host = cf_client_ip or (getattr(client, "host", "") if client is not None else "")
    client_tuple = (client_host, 0)  # Port is not available from Worker request
    
    # Build ASGI scope dictionary per ASGI 3.0 specification
    # This is what FastAPI/Starlette expects to receive
    scope: Dict[str, Any] = {
        "type": "http",  # ASGI message type
        "http_version": "1.1",  # HTTP version
        "asgi": {"version": "3.0"},  # ASGI version
        "method": request.method,  # GET, POST, etc.
        "scheme": split.scheme or "https",  # http or https (default to https for Workers)
        "root_path": "",  # Root path prefix (not used)
        "path": path or "/",  # URL path (decoded string)
        "raw_path": raw_path_bytes,  # URL path as raw bytes
        "query_string": raw_query_bytes,  # Query string as bytes (e.g., b"key=value")
        "client": client_tuple,  # (host: str, port: int) tuple
        "server": (split.hostname or "", split.port or (443 if split.scheme == "https" else 80)),
        "headers": headers,  # List of (name: bytes, value: bytes) tuples
    }

    # Read the full request body (handles empty bodies gracefully)
    body = await _extract_body(request)
    body_sent = False

    # ASGI receive callable: provides request body to the ASGI app
    # ASGI apps call this to get the request body in chunks
    async def receive() -> Dict[str, Any]:
        nonlocal body_sent
        if body_sent:
            # After body is sent, return disconnect message
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}
        body_sent = True
        # Send entire body in one chunk (more_body=False)
        # Note: This does NOT support streaming request bodies with more_body=True
        return {"type": "http.request", "body": body, "more_body": False}

    # Accumulate response data from ASGI app
    response_body = bytearray()
    status_code = 500  # Default to 500 if app doesn't send status
    response_headers: HeadersList = []

    # ASGI send callable: receives response messages from the ASGI app
    # ASGI apps call this to send response status, headers, and body
    async def send(message: Dict[str, Any]) -> None:
        nonlocal status_code, response_headers
        message_type = message["type"]
        if message_type == "http.response.start":
            # First message: status code and headers
            status_code = message["status"]
            response_headers = message.get("headers", [])
        elif message_type == "http.response.body":
            # Body chunks: accumulate all body bytes
            # Note: This only handles single-response bodies.
            # Streaming responses (more_body=True) are NOT supported in Phase 1.
            body_chunk = message.get("body", b"")
            response_body.extend(body_chunk)
        else:  # pragma: no cover - ASGI extensions
            # Ignore other message types (e.g., http.response.push)
            pass

    # Call the ASGI app with the scope and callables
    await app(scope, receive, send)

    # Convert ASGI response headers (bytes) to Python strings for Worker Response
    # Worker Response expects headers as (name: str, value: str) tuples
    py_headers = [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in response_headers
    ]
    
    # Create Cloudflare Worker Response object
    response_init = to_js({"status": status_code, "headers": py_headers})
    return Response.new(bytes(response_body), response_init)


__all__ = ["handle_worker_request"]
