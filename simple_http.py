"""Minimal HTTP client helpers built on urllib for environments without httpx."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


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


def request(
    method: str,
    url: str,
    *,
    headers: HeadersType = None,
    params: ParamsType = None,
    data: DataType = None,
    json: Optional[Any] = None,
    timeout: float = 10.0,
) -> SimpleResponse:
    """Perform a blocking HTTP request using urllib."""

    request_headers: Dict[str, str] = dict(headers or {})
    body = _prepare_body(data, json, request_headers)
    full_url = _build_url(url, params)
    req = Request(full_url, data=body, headers=request_headers, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            headers_dict = {k.lower(): v for k, v in resp.headers.items()}
            status = resp.getcode()
            final_url = resp.geturl()
            return SimpleResponse(status, headers_dict, content, final_url)
    except HTTPError as exc:
        content = exc.read() if exc.fp else b""
        headers_dict = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        response = SimpleResponse(exc.code, headers_dict, content, exc.geturl())
        raise HTTPStatusError(response) from None
    except URLError as exc:
        raise RequestError(str(exc)) from exc


async def async_request(**kwargs: Any) -> SimpleResponse:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: request(**kwargs))


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
