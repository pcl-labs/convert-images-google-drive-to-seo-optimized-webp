"""Minimal HTTP client helpers built on fetch (Workers) or urllib (local dev)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

# Import fetch and Request from Cloudflare Workers (js module)
try:
    from js import fetch as _worker_fetch, Request as JSRequest, Object as JSObject
except ImportError:
    _worker_fetch = None
    JSRequest = None
    JSObject = None


class RequestError(Exception):
    """Raised when a network error occurs."""


class HTTPStatusError(Exception):
    """Raised when the response status code is >= 400."""

    def __init__(self, response: "SimpleResponse") -> None:
        self.response = response
        message = f"HTTP {response.status_code}: {response.text[:200]}"
        super().__init__(message)


@dataclass
class SimpleResponse:
    status_code: int
    headers: Dict[str, str]
    content: bytes
    url: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPStatusError(self)


HeadersType = Optional[Mapping[str, str]]
ParamsType = Optional[Mapping[str, Union[str, int, float, bool]]]
DataType = Optional[Union[Mapping[str, Any], Iterable[tuple], bytes, str]]


def _prepare_body(
    data: DataType,
    json_body: Optional[Any],
    headers: MutableMapping[str, str],
) -> Optional[bytes]:
    if json_body is not None:
        headers.setdefault("Content-Type", "application/json")
        return json.dumps(json_body).encode("utf-8")
    if data is None:
        return None
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    return urlencode(data, doseq=True).encode("utf-8")


def _build_url(url: str, params: ParamsType) -> str:
    if not params:
        return url
    query = urlencode(params, doseq=True)
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def _validate_url_scheme(url: str) -> None:
    """Validate that URL scheme is http or https to prevent SSRF/local file access.
    
    Raises RequestError if scheme is missing or not allowed.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() if parsed.scheme else ""
    
    if not scheme:
        raise RequestError(f"URL scheme is required. Got: {url}")
    
    if scheme not in ("http", "https"):
        raise RequestError(f"Only http and https schemes are allowed. Got: {scheme}://...")


def request(
    method: str,
    url: str,
    *,
    headers: HeadersType = None,
    params: ParamsType = None,
    data: DataType = None,
    json: Optional[Any] = None,
    timeout: float = 10.0,
    stream_to=None,
    chunk_size: int = 64 * 1024,
) -> SimpleResponse:
    """Perform a blocking HTTP request using urllib."""

    request_headers: Dict[str, str] = dict(headers or {})
    body = _prepare_body(data, json, request_headers)
    full_url = _build_url(url, params)
    
    # Validate URL scheme to prevent SSRF/local file access
    _validate_url_scheme(full_url)
    
    req = Request(full_url, data=body, headers=request_headers, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            headers_dict = {k.lower(): v for k, v in resp.headers.items()}
            status = resp.getcode()
            final_url = resp.geturl()
            if stream_to is not None:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    stream_to.write(chunk)
                content = b""
            else:
                content = resp.read()
            return SimpleResponse(status, headers_dict, content, final_url)
    except HTTPError as exc:
        # Safely read content, fallback to empty bytes if reading fails
        try:
            content = exc.read() if exc.fp else b""
        except Exception:
            content = b""
        
        # Safely get headers using getattr
        headers = getattr(exc, "headers", None)
        headers_dict = {k.lower(): v for k, v in headers.items()} if headers else {}
        
        # Safely get URL - try geturl() first, then fallback to url attribute
        url = getattr(exc, "geturl", lambda: None)()
        if url is None:
            url = getattr(exc, "url", None)
        
        # Safely get status code
        status_code = getattr(exc, "code", 500)
        
        response = SimpleResponse(status_code, headers_dict, content, url or "")
        raise HTTPStatusError(response) from None
    except URLError as exc:
        raise RequestError(str(exc)) from exc


async def _fetch_request(
    method: str,
    url: str,
    *,
    headers: HeadersType = None,
    params: ParamsType = None,
    data: DataType = None,
    json_body: Optional[Any] = None,
    timeout: float = 10.0,
) -> SimpleResponse:
    """Perform an async HTTP request using fetch API (Cloudflare Workers)."""
    if _worker_fetch is None:
        raise RuntimeError("fetch API not available - this code requires Cloudflare Workers runtime")
    
    import logging
    logger = logging.getLogger(__name__)
    
    # Build URL with params
    full_url = _build_url(url, params)
    
    # Validate URL scheme to prevent SSRF/local file access
    _validate_url_scheme(full_url)
    
    # Prepare headers and body using standard approach
    request_headers: Dict[str, str] = dict(headers or {})
    body = _prepare_body(data, json_body, request_headers)
    
    # Import AbortController and setTimeout from js module for timeout handling
    try:
        from js import AbortController, setTimeout, clearTimeout
    except ImportError:
        # Fallback if AbortController is not available
        AbortController = None
        setTimeout = None
        clearTimeout = None
    
    # Create AbortController for timeout
    controller = None
    timeout_id = None
    
    try:
        if AbortController is not None:
            controller = AbortController.new()
        
        # Build fetch options as Python dict first
        fetch_options_dict = {
            "method": method.upper(),
        }
        
        # Add abort signal to fetch options if controller is available
        if controller is not None:
            fetch_options_dict["signal"] = controller.signal
        
        # Convert headers dict to JavaScript object explicitly
        if JSObject is not None and request_headers:
            fetch_options_dict["headers"] = JSObject.fromEntries([
                [k, v] for k, v in request_headers.items()
            ])
        else:
            fetch_options_dict["headers"] = request_headers
        
        # Convert body from bytes to string for text-based content types
        # Cloudflare Workers Python fetch API (via Pyodide) expects string for form-encoded and JSON
        if body is not None:
            content_type = request_headers.get("Content-Type", "")
            if isinstance(body, bytes):
                # For text-based content types, decode bytes to string
                if content_type in ("application/x-www-form-urlencoded", "application/json"):
                    fetch_options_dict["body"] = body.decode("utf-8")
                else:
                    # Keep binary data as bytes
                    fetch_options_dict["body"] = body
            else:
                fetch_options_dict["body"] = body
        
        # Convert entire fetch_options dict to JavaScript object
        if JSObject is not None:
            fetch_options = JSObject.fromEntries([
                [k, v] for k, v in fetch_options_dict.items()
            ])
        else:
            fetch_options = fetch_options_dict
        
        # Set up timeout timer to abort the request
        if controller is not None and setTimeout is not None:
            def abort_request():
                try:
                    controller.abort()
                except Exception:
                    pass
            
            timeout_id = setTimeout(abort_request, int(timeout * 1000))
        
        # Call fetch with URL and options
        try:
            response = await _worker_fetch(full_url, fetch_options)
            # Clear timeout immediately after successful fetch
            if timeout_id is not None and clearTimeout is not None:
                try:
                    clearTimeout(timeout_id)
                    timeout_id = None
                except Exception:
                    pass
        except Exception as exc:
            # Clear timeout on error as well
            if timeout_id is not None and clearTimeout is not None:
                try:
                    clearTimeout(timeout_id)
                    timeout_id = None
                except Exception:
                    pass
            
            # Check if error is due to abort (timeout)
            # In JavaScript, aborted fetch throws DOMException with name "AbortError"
            # In Pyodide, this might be wrapped, so check both the exception type and signal state
            is_abort_error = False
            if controller is not None:
                try:
                    # Check if signal is aborted
                    if hasattr(controller.signal, 'aborted') and controller.signal.aborted:
                        is_abort_error = True
                    # Check exception name/type for AbortError
                    exc_name = getattr(exc, 'name', None) or getattr(exc, '__class__', {}).get('__name__', '')
                    if 'Abort' in str(exc_name) or 'AbortError' in str(exc):
                        is_abort_error = True
                except Exception:
                    pass
            
            if is_abort_error:
                raise RequestError(f"Request timeout after {timeout} seconds") from exc
            
            logger.error(
                "Fetch call failed: url=%s, method=%s, error=%s",
                full_url,
                method.upper(),
                exc,
                exc_info=True,
            )
            raise
    finally:
        # Clean up timeout timer as final safety net
        if timeout_id is not None and clearTimeout is not None:
            try:
                clearTimeout(timeout_id)
            except Exception:
                pass
    
    # Extract response data
    status = response.status
    
    # Convert Headers object to dict
    response_headers = {}
    for key in response.headers.keys():
        value = response.headers.get(key)
        if value:
            response_headers[key.lower()] = value
    
    # Read response body
    content = await response.bytes()
    if not isinstance(content, bytes):
        content = bytes(content)
    
    return SimpleResponse(status, response_headers, content, full_url)


async def async_request(
    method: str,
    url: str,
    *,
    headers: HeadersType = None,
    params: ParamsType = None,
    data: DataType = None,
    json: Optional[Any] = None,
    timeout: float = 10.0,
    **kwargs: Any
) -> SimpleResponse:
    """Perform an async HTTP request using fetch API (Cloudflare Workers)."""
    if _worker_fetch is None:
        raise RuntimeError(
            "fetch API not available. This code requires Cloudflare Workers runtime. "
            "For local development, use a different HTTP client or run via 'wrangler dev'."
        )
    
    return await _fetch_request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        data=data,
        json_body=json,
        timeout=timeout,
    )


class AsyncSimpleClient:
    def __init__(self, *, timeout: float = 10.0, base_url: Optional[str] = None) -> None:
        self.timeout = timeout
        self.base_url = base_url.rstrip("/") if base_url else None

    def _resolve_url(self, url: str) -> str:
        if self.base_url and not url.startswith("http"):
            return urljoin(f"{self.base_url}/", url.lstrip("/"))
        return url

    async def request(self, method: str, url: str, **kwargs: Any) -> SimpleResponse:
        resolved = self._resolve_url(url)
        return await async_request(method=method, url=resolved, timeout=self.timeout, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> SimpleResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> SimpleResponse:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> SimpleResponse:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> SimpleResponse:
        return await self.request("DELETE", url, **kwargs)

    async def __aenter__(self) -> "AsyncSimpleClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class SimpleClient:
    def __init__(self, *, timeout: float = 10.0, base_url: Optional[str] = None) -> None:
        self.timeout = timeout
        self.base_url = base_url.rstrip("/") if base_url else None

    def _resolve_url(self, url: str) -> str:
        if self.base_url and not url.startswith("http"):
            return urljoin(f"{self.base_url}/", url.lstrip("/"))
        return url

    def request(self, method: str, url: str, **kwargs: Any) -> SimpleResponse:
        resolved = self._resolve_url(url)
        return request(method=method, url=resolved, timeout=self.timeout, **kwargs)

    def get(self, url: str, **kwargs: Any) -> SimpleResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> SimpleResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> SimpleResponse:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> SimpleResponse:
        return self.request("DELETE", url, **kwargs)

    def __enter__(self) -> "SimpleClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None
