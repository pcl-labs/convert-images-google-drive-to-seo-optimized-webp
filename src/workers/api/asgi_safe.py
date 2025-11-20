"""
ASGI middleware to make Starlette/FastAPI compatible with Cloudflare's strict ASGI adapter.

This wrapper prevents InvalidStateError by ensuring only one complete response per request,
ignoring any duplicate send attempts that Starlette's error recovery might attempt.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

# ASGI type aliases
ASGIApp = Callable[
    [
        Dict[str, Any],
        Callable[[], Awaitable[Dict[str, Any]]],
        Callable[[Dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]

Receive = Callable[[], Awaitable[Dict[str, Any]]]
Send = Callable[[Dict[str, Any]], Awaitable[None]]


class SingleResponseMiddleware:
    """
    ASGI wrapper to make Starlette/FastAPI behave with Cloudflare's ASGI adapter.
    
    Guarantees:
    - For HTTP scopes, only the *first* complete response (the first `http.response.body`
      with `more_body=False`) is forwarded to the underlying `send`.
    - Any subsequent `http.response.start` / `http.response.body` messages are ignored
      (optionally logged), instead of being forwarded and causing `InvalidStateError`
      in the Cloudflare adapter.
    
    For non-HTTP scopes (websocket, lifespan), this is a no-op pass-through.
    
    This solves the race condition where:
    1. Starlette successfully sends a response
    2. An exception occurs (task group cleanup, middleware error)
    3. Starlette's error handler attempts to send an error response
    4. Cloudflare's adapter rejects it (Future already done) → InvalidStateError
    
    By ignoring duplicate sends after completion, we satisfy Starlette's error recovery
    pattern while respecting Cloudflare's single-completion Future model.
    """
    
    def __init__(self, app: ASGIApp):
        self.app = app
    
    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        scope_type = scope.get("type")
        
        # Only wrap normal HTTP requests; everything else passes through untouched
        if scope_type != "http":
            await self.app(scope, receive, send)
            return
        
        # Track response state for HTTP requests
        response_started = False
        response_complete = False
        
        async def safe_send(message: Dict[str, Any]) -> None:
            nonlocal response_started, response_complete
            
            msg_type = message.get("type")
            
            if msg_type == "http.response.start":
                # Drop duplicate starts after the first one
                if response_started:
                    # Duplicate start ignored - this prevents InvalidStateError
                    # when Starlette's error handler tries to send an error response
                    # after a successful response was already sent
                    return
                response_started = True
            
            elif msg_type == "http.response.body":
                # If we've already seen a terminal body, ignore further bodies
                if response_complete:
                    # Duplicate body after completion ignored - this is the key fix
                    # Starlette's error recovery attempts are silently dropped
                    return
                
                # If this is the terminal body, mark as complete BEFORE sending
                # This ensures we catch any race conditions
                more_body = bool(message.get("more_body", False))
                if not more_body:
                    response_complete = True
            
            # Forward the message to the real send (Cloudflare ASGI adapter)
            # Wrap in try/except as a defensive measure in case our checks miss something
            try:
                await send(message)
            except Exception as e:
                # If we get InvalidStateError or any other error after response is complete,
                # it means Cloudflare's adapter already completed the Future.
                # This is expected behavior - silently ignore it.
                if response_complete:
                    # Expected: duplicate send after completion, ignore
                    return
                # Unexpected error before completion - re-raise
                raise
        
        # Delegate to the underlying app with the wrapped send
        await self.app(scope, receive, safe_send)

