"""Lightweight Google API clients implemented with urllib helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Dict, Optional

from api.simple_http import HTTPStatusError, RequestError, SimpleClient, SimpleResponse, AsyncSimpleClient

logger = logging.getLogger(__name__)


class GoogleAPIError(Exception):
    """Base error for Google API issues."""


class GoogleHTTPError(GoogleAPIError):
    """HTTP error raised when Google responds with an error code."""

    def __init__(self, status_code: int, message: str, *, payload: Optional[str] = None):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"HTTP {status_code}: {message}")


@dataclass
class OAuthToken:
    """Simple structure representing an OAuth token bundle."""

    access_token: str
    refresh_token: Optional[str] = None
    expiry: Optional[datetime] = None
    token_type: str = "Bearer"

    def is_expired(self, *, skew_seconds: int = 60) -> bool:
        if not self.expiry:
            return False
        now = datetime.now(timezone.utc)
        return now >= (self.expiry - timedelta(seconds=skew_seconds))


class GoogleAPISession:
    """Minimal session that injects OAuth headers automatically."""

    def __init__(self, base_url: str, token: OAuthToken, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = SimpleClient(base_url=self.base_url, timeout=timeout)

    def _inject_headers(self, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        merged = {"Accept": "application/json"}
        if headers:
            merged.update(headers)
        auth_token = self.token.token_type or "Bearer"
        merged["Authorization"] = f"{auth_token} {self.token.access_token}"
        return merged

    def request(self, method: str, path: str, *, chunk_size: int = 64 * 1024, stream_to=None, **kwargs) -> SimpleResponse:
        headers = self._inject_headers(kwargs.pop("headers", None))
        try:
            response = self._client.request(
                method,
                path,
                headers=headers,
                stream_to=stream_to,
                chunk_size=chunk_size,
                **kwargs,
            )
        except HTTPStatusError as exc:
            raise GoogleHTTPError(exc.response.status_code, exc.response.text, payload=exc.response.text) from exc
        except RequestError as exc:
            raise GoogleAPIError(f"Network error: {exc}") from exc
        return response

    def close(self) -> None:
        """Close the underlying HTTP client and release resources."""
        if self._client is None:
            return
        
        try:
            # Try synchronous close() method
            if hasattr(self._client, "close"):
                self._client.close()
            # Note: If client has aclose() but not close(), we can't await it from sync context
            # In that case, we'll just release the reference below
        except Exception as exc:
            logger.warning("Error closing HTTP client: %s", exc, exc_info=True)
        finally:
            self._client = None


def _is_workers_runtime() -> bool:
    """Return True when running inside the Cloudflare Workers Python runtime.

    We detect this by attempting to import ``js.fetch`` which is only available
    in the Workers/Pyodide environment. This helper is cheap and safe to call.
    """
    try:  # pragma: no cover - environment specific
        from js import fetch as _f  # type: ignore
    except ImportError:  # pragma: no cover - standard Python
        return False
    return _f is not None


def _run_or_schedule_close(awaitable: Awaitable[Any], label: str) -> None:
    async def _runner() -> None:
        try:
            await awaitable
        except Exception:
            logger.warning("Error closing %s", label, exc_info=True)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(_runner())
        except Exception:
            logger.warning("Error closing %s", label, exc_info=True)
    else:
        loop.create_task(_runner())


class AsyncGoogleAPISession:
    """Async Google API session backed by AsyncSimpleClient (Workers runtime).

    This mirrors GoogleAPISession but issues requests via the Workers ``fetch``
    API through AsyncSimpleClient, which is required for Cloudflare Python
    Workers where urllib/urlopen are not appropriate.
    """

    def __init__(self, base_url: str, token: OAuthToken, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = AsyncSimpleClient(base_url=self.base_url, timeout=timeout)

    def _inject_headers(self, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        merged: Dict[str, str] = {"Accept": "application/json"}
        if headers:
            merged.update(headers)
        auth_token = self.token.token_type or "Bearer"
        merged["Authorization"] = f"{auth_token} {self.token.access_token}"
        return merged

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        parse_json: bool = True,
    ) -> Any:
        merged_headers = self._inject_headers(headers)
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=merged_headers,
            )
        except HTTPStatusError as exc:
            raise GoogleHTTPError(
                exc.response.status_code,
                exc.response.text,
                payload=exc.response.text,
            ) from exc
        except RequestError as exc:
            raise GoogleAPIError(f"Network error: {exc}") from exc

        if not parse_json:
            return response

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise GoogleAPIError(
                f"Google API returned invalid JSON: {response.text[:200]}"
            ) from exc

    async def aclose(self) -> None:
        if not self._client:
            return
        client = self._client
        self._client = None
        try:
            close = getattr(client, "aclose", None)
            if callable(close):
                await close()
            else:
                sync_close = getattr(client, "close", None)
                if callable(sync_close):
                    sync_close()
        except Exception as exc:
            logger.warning("Error closing async HTTP client: %s", exc, exc_info=True)


class GoogleDriveClient:
    """Minimal Google Drive v3 client for listing/uploading/downloading files."""

    def __init__(self, token: OAuthToken):
        self.token = token
        self._metadata_session = GoogleAPISession("https://www.googleapis.com/drive/v3", token)
        self._upload_session = GoogleAPISession("https://www.googleapis.com/upload/drive/v3", token)
        self._async_metadata_session: Optional[AsyncGoogleAPISession] = None
        if _is_workers_runtime():  # pragma: no cover - Workers specific
            self._async_metadata_session = AsyncGoogleAPISession("https://www.googleapis.com/drive/v3", token)
        self._files_resource = _GoogleDriveFilesResource(self._metadata_session)

    def list_folder_files(
        self,
        folder_id: str,
        *,
        page_token: Optional[str] = None,
        fields: str = "nextPageToken, files(id, name, mimeType)",
    ) -> Dict[str, Any]:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "spaces": "drive",
            "fields": fields,
        }
        if page_token:
            params["pageToken"] = page_token
        response = self._metadata_session.request("GET", "/files", params=params)
        return response.json()

    async def list_files_async(
        self,
        *,
        params: Optional[Dict[str, Any]] = None,
        fields: str = "files(id,name,webViewLink)",
    ) -> Dict[str, Any]:
        """Generic async /files list helper for Workers.

        When AsyncGoogleAPISession is available (Workers runtime), use it so
        requests go through AsyncSimpleClient/fetch. Otherwise, run the
        existing synchronous client in a thread.
        """
        if params is None:
            params = {}
        if fields:
            params = {**params, "fields": fields}
        if self._async_metadata_session:
            return await self._async_metadata_session.request("GET", "/files", params=params)
        # Fallback: run sync request in a thread
        response: SimpleResponse = await asyncio.to_thread(
            self._metadata_session.request,
            "GET",
            "/files",
            params=params,
        )
        return response.json()

    async def list_folder_files_async(
        self,
        folder_id: str,
        *,
        page_token: Optional[str] = None,
        fields: str = "nextPageToken, files(id, name, mimeType)",
    ) -> Dict[str, Any]:
        """Async variant of list_folder_files for Workers runtime.

        Uses AsyncGoogleAPISession when available; otherwise falls back to the
        synchronous implementation executed in a thread.
        """
        if not self._async_metadata_session:
            return await asyncio.to_thread(
                self.list_folder_files,
                folder_id,
                page_token=page_token,
                fields=fields,
            )

        params: Dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "spaces": "drive",
            "fields": fields,
        }
        if page_token:
            params["pageToken"] = page_token
        return await self._async_metadata_session.request("GET", "/files", params=params)

    def download_file(self, file_id: str, file_obj) -> None:
        self._metadata_session.request(
            "GET",
            f"/files/{file_id}",
            params={"alt": "media"},
            headers={"Accept": "application/octet-stream"},
            stream_to=file_obj,
        )

    def upload_file(self, folder_id: str, filename: str, file_obj, *, mimetype: str = "application/octet-stream") -> Dict[str, Any]:
        metadata = {"name": filename, "parents": [folder_id]}
        boundary = uuid.uuid4().hex

        def _part(name: str, filename: Optional[str], content_type: Optional[str], value) -> bytes:
            # For multipart/related, only emit Content-Type for each part
            headers = ""
            if content_type:
                headers += f"Content-Type: {content_type}\r\n"
            headers += "\r\n"
            if hasattr(value, "read"):
                data = value.read()
            elif isinstance(value, bytes):
                data = value
            else:
                data = str(value).encode("utf-8")
            return headers.encode("utf-8") + data + b"\r\n"

        body = bytearray()
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            _part(
                "metadata",
                "metadata",
                "application/json; charset=UTF-8",
                json.dumps(metadata),
            )
        )
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        body.extend(_part("media", filename, mimetype, file_obj))
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        response = self._upload_session.request(
            "POST",
            "/files?uploadType=multipart",
            data=bytes(body),
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        return response.json()

    def delete_file(self, file_id: str) -> None:
        self._metadata_session.request("DELETE", f"/files/{file_id}")

    def get_file_metadata(self, file_id: str, fields: str = "id,name,mimeType") -> Dict[str, Any]:
        response = self._metadata_session.request("GET", f"/files/{file_id}", params={"fields": fields})
        return response.json()

    async def get_file_metadata_async(self, file_id: str, *, fields: str = "id,name,webViewLink") -> Dict[str, Any]:
        """Async helper for GET /files/{fileId} metadata calls."""
        params: Dict[str, Any] = {"fields": fields}
        path = f"/files/{file_id}"
        if self._async_metadata_session:
            return await self._async_metadata_session.request("GET", path, params=params)
        response: SimpleResponse = await asyncio.to_thread(
            self._metadata_session.request,
            "GET",
            path,
            params=params,
        )
        return response.json()

    async def create_file_async(self, *, body: Dict[str, Any], fields: str = "id,name,webViewLink") -> Dict[str, Any]:
        """Async helper for POST /files to create Drive folders/files."""
        params: Dict[str, Any] = {"fields": fields}
        if self._async_metadata_session:
            return await self._async_metadata_session.request(
                "POST",
                "/files",
                params=params,
                json_body=body,
            )
        response: SimpleResponse = await asyncio.to_thread(
            self._metadata_session.request,
            "POST",
            "/files",
            params=params,
            json=body,
        )
        return response.json()

    async def update_file_async(
        self,
        file_id: str,
        *,
        body: Dict[str, Any],
        fields: str = "id,headRevisionId,webViewLink",
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Async helper for PATCH /files/{fileId} operations."""
        params: Dict[str, Any] = {"fields": fields}
        if extra_params:
            params.update({k: v for k, v in extra_params.items() if v is not None})
        path = f"/files/{file_id}"
        if self._async_metadata_session:
            return await self._async_metadata_session.request(
                "PATCH",
                path,
                params=params,
                json_body=body,
            )
        response: SimpleResponse = await asyncio.to_thread(
            self._metadata_session.request,
            "PATCH",
            path,
            params=params,
            json=body,
        )
        return response.json()

    def files(self) -> "_GoogleDriveFilesResource":
        return self._files_resource

    def close(self) -> None:
        self._metadata_session.close()
        self._upload_session.close()
        self._close_async_metadata_session()

    async def aclose(self) -> None:
        self._metadata_session.close()
        self._upload_session.close()
        session = self._async_metadata_session
        self._async_metadata_session = None
        if session:
            try:
                await session.aclose()
            except Exception:
                logger.warning("Error closing async Drive session", exc_info=True)

    def _close_async_metadata_session(self) -> None:
        session = self._async_metadata_session
        if not session:
            return
        self._async_metadata_session = None
        _run_or_schedule_close(session.aclose(), "Drive async metadata session")


class GoogleDocsRequest:
    """Wrapper mimicking googleapiclient HttpRequest interface for Docs API calls."""

    def __init__(
        self,
        session: GoogleAPISession,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ):
        self._session = session
        self._method = method
        self._path = path
        self._params = params or None
        self._json_body = json_body or None

    def execute(self) -> Dict[str, Any]:
        response = self._session.request(
            self._method,
            self._path,
            params=self._params,
            json=self._json_body,
        )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - defensive guard
            raise GoogleAPIError(f"Docs API returned invalid JSON: {response.text[:200]}") from exc


class _GoogleDocsDocumentsResource:
    def __init__(self, session: GoogleAPISession):
        self._session = session

    @staticmethod
    def _require_doc_id(document_id: str) -> str:
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("documentId is required for Docs API call")
        return document_id

    def create(self, body: Optional[Dict[str, Any]] = None) -> GoogleDocsRequest:
        return GoogleDocsRequest(self._session, "POST", "/documents", json_body=body)

    def get(self, documentId: str) -> GoogleDocsRequest:
        doc_id = self._require_doc_id(documentId)
        return GoogleDocsRequest(self._session, "GET", f"/documents/{doc_id}")

    def batchUpdate(self, documentId: str, body: Optional[Dict[str, Any]] = None) -> GoogleDocsRequest:
        doc_id = self._require_doc_id(documentId)
        return GoogleDocsRequest(self._session, "POST", f"/documents/{doc_id}:batchUpdate", json_body=body)


class GoogleDocsClient:
    """Minimal Docs API client that mimics the subset of googleapiclient used in the worker."""

    def __init__(self, token: OAuthToken):
        self._session = GoogleAPISession("https://docs.googleapis.com/v1", token)
        self._documents_resource = _GoogleDocsDocumentsResource(self._session)

    def documents(self) -> _GoogleDocsDocumentsResource:
        return self._documents_resource

    def close(self) -> None:
        self._session.close()


class GoogleDriveRequest:
    """Request shim for Drive API operations."""

    def __init__(
        self,
        session: GoogleAPISession,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ):
        self._session = session
        self._method = method
        self._path = path
        self._params = params or None
        self._json_body = json_body or None

    def execute(self) -> Dict[str, Any]:
        response = self._session.request(
            self._method,
            self._path,
            params=self._params,
            json=self._json_body,
        )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover
            raise GoogleAPIError(f"Drive API returned invalid JSON: {response.text[:200]}") from exc


class _GoogleDriveFilesResource:
    """Subset of Drive files resource methods used by the worker code."""

    def __init__(self, session: GoogleAPISession):
        self._session = session

    @staticmethod
    def _build_params(fields: Optional[str], extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = fields
        if extra:
            params.update({k: v for k, v in extra.items() if v is not None})
        return params

    @staticmethod
    def _require_file_id(file_id: str) -> str:
        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError("fileId is required for Drive API call")
        return file_id

    def list(self, fields: Optional[str] = None, **kwargs) -> GoogleDriveRequest:
        params = self._build_params(fields, kwargs)
        return GoogleDriveRequest(self._session, "GET", "/files", params=params)

    def get(self, fileId: str, fields: Optional[str] = None, **kwargs) -> GoogleDriveRequest:
        file_id = self._require_file_id(fileId)
        params = self._build_params(fields, kwargs)
        return GoogleDriveRequest(self._session, "GET", f"/files/{file_id}", params=params)

    def create(self, body: Optional[Dict[str, Any]] = None, fields: Optional[str] = None, **kwargs) -> GoogleDriveRequest:
        params = self._build_params(fields, kwargs)
        return GoogleDriveRequest(self._session, "POST", "/files", params=params, json_body=body)

    def update(
        self,
        fileId: str,
        body: Optional[Dict[str, Any]] = None,
        fields: Optional[str] = None,
        **kwargs,
    ) -> GoogleDriveRequest:
        file_id = self._require_file_id(fileId)
        params = self._build_params(fields, kwargs)
        return GoogleDriveRequest(self._session, "PATCH", f"/files/{file_id}", params=params, json_body=body)


class YouTubeClient:
    """Minimal YouTube Data API client."""

    def __init__(self, token: OAuthToken):
        self._session = GoogleAPISession("https://youtube.googleapis.com/youtube/v3", token)
        self._async_session: Optional[AsyncGoogleAPISession] = None
        if _is_workers_runtime():  # pragma: no cover - Workers specific
            self._async_session = AsyncGoogleAPISession("https://youtube.googleapis.com/youtube/v3", token)

    def fetch_video(self, video_id: str) -> Dict[str, Any]:
        response = self._session.request(
            "GET",
            "/videos",
            params={"part": "snippet,contentDetails,status", "id": video_id},
        )
        return response.json()

    async def fetch_video_async(self, video_id: str) -> Dict[str, Any]:
        """Async variant of fetch_video for Workers runtime.

        Uses AsyncGoogleAPISession when available; otherwise executes the
        synchronous fetch_video implementation in a worker thread.
        """
        if not self._async_session:
            return await asyncio.to_thread(self.fetch_video, video_id)
        return await self._async_session.request(
            "GET",
            "/videos",
            params={"part": "snippet,contentDetails,status", "id": video_id},
        )

    def list_captions(self, video_id: str) -> Dict[str, Any]:
        response = self._session.request(
            "GET",
            "/captions",
            params={"part": "id,snippet", "videoId": video_id},
        )
        return response.json()

    async def list_captions_async(self, video_id: str) -> Dict[str, Any]:
        """Async variant of list_captions for Workers runtime.

        Uses AsyncGoogleAPISession when available; otherwise executes the
        synchronous list_captions implementation in a worker thread.
        """
        if not self._async_session:
            return await asyncio.to_thread(self.list_captions, video_id)
        return await self._async_session.request(
            "GET",
            "/captions",
            params={"part": "id,snippet", "videoId": video_id},
        )

    def download_caption(self, caption_id: str, *, format: str = "srt") -> str:
        response = self._session.request(
            "GET",
            f"/captions/{caption_id}",
            params={"tfmt": format},
            headers={"Accept": "application/octet-stream"},
        )
        if response.headers.get("content-type", "").startswith("application/json"):
            # YouTube may return JSON errors; raise a descriptive error
            raise GoogleHTTPError(response.status_code, response.text, payload=response.text)
        return response.text

    async def download_caption_async(self, caption_id: str, *, format: str = "srt") -> str:
        """Async variant of download_caption for Workers runtime.

        Uses the async HTTP client when available; otherwise runs the sync
        implementation in a worker thread. Returns caption text (SRT or
        requested format) or raises GoogleHTTPError/GoogleAPIError on failure.
        """
        if not self._async_session:
            return await asyncio.to_thread(self.download_caption, caption_id, format=format)

        response = await self._async_session.request(
            "GET",
            f"/captions/{caption_id}",
            params={"tfmt": format},
            headers={"Accept": "application/octet-stream"},
            parse_json=False,
        )

        if response.headers.get("content-type", "").startswith("application/json"):
            raise GoogleHTTPError(response.status_code, response.text, payload=response.text)
        return response.text

    def close(self) -> None:
        self._session.close()
        self._schedule_async_close()

    async def aclose(self) -> None:
        self._session.close()
        session = self._async_session
        self._async_session = None
        if session:
            try:
                await session.aclose()
            except Exception:
                logger.warning("Error closing async YouTube session", exc_info=True)

    def _schedule_async_close(self) -> None:
        session = self._async_session
        if not session:
            return
        self._async_session = None
        _run_or_schedule_close(session.aclose(), "YouTube async session")


__all__ = [
    "GoogleAPIError",
    "GoogleHTTPError",
    "GoogleDocsClient",
    "GoogleDriveClient",
    "YouTubeClient",
    "OAuthToken",
]
