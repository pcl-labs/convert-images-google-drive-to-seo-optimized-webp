"""
Test for SingleResponseMiddleware - ensures only one complete response per request.

This test verifies that the middleware prevents InvalidStateError by:
1. Allowing the first complete response through
2. Ignoring any subsequent send attempts after completion
"""

import pytest
from typing import Dict, Any, List
from src.workers.api.asgi_safe import SingleResponseMiddleware


class MockSend:
    """Mock send callable that records all messages."""
    
    def __init__(self):
        self.messages: List[Dict[str, Any]] = []
        self.call_count = 0
    
    async def __call__(self, message: Dict[str, Any]) -> None:
        self.call_count += 1
        self.messages.append(message.copy())


class DeliberateDoubleSendApp:
    """ASGI app that deliberately sends a response twice (simulates Starlette's error recovery)."""
    
    async def __call__(self, scope: Dict[str, Any], receive, send) -> None:
        # Send first complete response
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"status": "ok"}',
            "more_body": False,  # First response complete
        })
        
        # Simulate Starlette's error handler attempting a second send
        # This would cause InvalidStateError without the wrapper
        await send({
            "type": "http.response.start",
            "status": 500,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"error": "recovery attempt"}',
            "more_body": False,
        })


class NormalApp:
    """Normal ASGI app that sends one response correctly."""
    
    async def __call__(self, scope: Dict[str, Any], receive, send) -> None:
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"status": "ok"}',
            "more_body": True,
        })
        await send({
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        })


@pytest.mark.asyncio
async def test_single_response_middleware_prevents_double_send():
    """Test that SingleResponseMiddleware prevents InvalidStateError from double sends."""
    
    # Create app that tries to send twice
    inner_app = DeliberateDoubleSendApp()
    wrapped_app = SingleResponseMiddleware(inner_app)
    
    # Create mock send
    mock_send = MockSend()
    
    # Create HTTP scope
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "scheme": "http",
        "server": ("localhost", 8000),
    }
    
    # Call the wrapped app
    await wrapped_app(scope, lambda: None, mock_send)
    
    # Verify: Only the FIRST response should be forwarded
    assert mock_send.call_count == 2, "Should have received exactly 2 messages (start + body)"
    
    messages = mock_send.messages
    assert len(messages) == 2, "Should have exactly 2 messages"
    
    # First message should be response.start
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200, "First response should be 200"
    
    # Second message should be the first response body (with more_body=False)
    assert messages[1]["type"] == "http.response.body"
    assert messages[1]["body"] == b'{"status": "ok"}'
    assert messages[1].get("more_body", False) is False, "First response should be complete"
    
    # The second send attempt (500 error) should be ignored
    # We should NOT see a 500 response.start or the error body


@pytest.mark.asyncio
async def test_single_response_middleware_allows_normal_responses():
    """Test that SingleResponseMiddleware doesn't interfere with normal single responses."""
    
    inner_app = NormalApp()
    wrapped_app = SingleResponseMiddleware(inner_app)
    
    mock_send = MockSend()
    
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "scheme": "http",
        "server": ("localhost", 8000),
    }
    
    await wrapped_app(scope, lambda: None, mock_send)
    
    # Normal app sends: start, body (more_body=True), body (more_body=False)
    assert mock_send.call_count == 3, "Should receive all 3 messages from normal app"
    
    messages = mock_send.messages
    assert messages[0]["type"] == "http.response.start"
    assert messages[1]["type"] == "http.response.body"
    assert messages[1].get("more_body", False) is True
    assert messages[2]["type"] == "http.response.body"
    assert messages[2].get("more_body", False) is False


@pytest.mark.asyncio
async def test_single_response_middleware_passes_through_non_http_scopes():
    """Test that non-HTTP scopes (websocket, lifespan) pass through unchanged."""
    
    class LifespanApp:
        async def __call__(self, scope, receive, send):
            await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
    
    inner_app = LifespanApp()
    wrapped_app = SingleResponseMiddleware(inner_app)
    
    mock_send = MockSend()
    
    scope = {"type": "lifespan"}
    
    await wrapped_app(scope, lambda: None, mock_send)
    
    # Should pass through all messages for non-HTTP scopes
    assert mock_send.call_count == 2
    assert mock_send.messages[0]["type"] == "lifespan.startup.complete"
    assert mock_send.messages[1]["type"] == "lifespan.shutdown.complete"


@pytest.mark.asyncio
async def test_single_response_middleware_ignores_duplicate_starts():
    """Test that duplicate http.response.start messages are ignored after the first."""
    
    class DuplicateStartApp:
        async def __call__(self, scope, receive, send):
            # Send start twice
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.start", "status": 500, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})
    
    inner_app = DuplicateStartApp()
    wrapped_app = SingleResponseMiddleware(inner_app)
    
    mock_send = MockSend()
    
    scope = {"type": "http", "method": "GET", "path": "/"}
    
    await wrapped_app(scope, lambda: None, mock_send)
    
    # Should only see first start, not the duplicate
    assert mock_send.call_count == 2, "Should receive start + body, not duplicate start"
    messages = mock_send.messages
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200, "Should use first start, not second"
    assert messages[1]["type"] == "http.response.body"


@pytest.mark.asyncio
async def test_single_response_middleware_ignores_bodies_after_completion():
    """Test that http.response.body messages are ignored after completion."""
    
    class ExtraBodyApp:
        async def __call__(self, scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"first", "more_body": False})
            # Try to send more after completion
            await send({"type": "http.response.body", "body": b"second", "more_body": False})
            await send({"type": "http.response.body", "body": b"third", "more_body": False})
    
    inner_app = ExtraBodyApp()
    wrapped_app = SingleResponseMiddleware(inner_app)
    
    mock_send = MockSend()
    
    scope = {"type": "http", "method": "GET", "path": "/"}
    
    await wrapped_app(scope, lambda: None, mock_send)
    
    # Should only see first body, not the extras
    assert mock_send.call_count == 2, "Should receive start + first body only"
    messages = mock_send.messages
    assert messages[1]["body"] == b"first", "Should only forward first body"
    assert len([m for m in messages if m["type"] == "http.response.body"]) == 1


@pytest.mark.asyncio
async def test_single_response_middleware_handles_invalid_state_error():
    """Test that InvalidStateError from Cloudflare adapter is caught and ignored after completion."""
    
    class RaisesAfterCompleteApp:
        async def __call__(self, scope, receive, send):
            # Send first complete response
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})
            # Try to send another (simulating Starlette error handler)
            await send({"type": "http.response.start", "status": 500, "headers": []})
            await send({"type": "http.response.body", "body": b"error", "more_body": False})
    
    # Mock send that raises InvalidStateError on second call after first completion
    call_count = 0
    
    async def mock_send_raises(message):
        nonlocal call_count
        call_count += 1
        msg_type = message.get("type")
        # Simulate Cloudflare adapter: raise InvalidStateError on second complete body
        if msg_type == "http.response.body" and not message.get("more_body", False) and call_count > 2:
            import asyncio
            raise asyncio.exceptions.InvalidStateError("invalid state")
        # Otherwise, just record the message
        mock_send.messages.append(message.copy())
    
    mock_send = MockSend()
    mock_send.__call__ = mock_send_raises
    
    inner_app = RaisesAfterCompleteApp()
    wrapped_app = SingleResponseMiddleware(inner_app)
    
    scope = {"type": "http", "method": "GET", "path": "/"}
    
    # Should not raise - InvalidStateError should be caught and ignored
    await wrapped_app(scope, lambda: None, mock_send_raises)
    
    # Should have received first response only
    assert len(mock_send.messages) == 2, "Should receive start + first body only"
    assert mock_send.messages[0]["type"] == "http.response.start"
    assert mock_send.messages[0]["status"] == 200
    assert mock_send.messages[1]["body"] == b"ok"

