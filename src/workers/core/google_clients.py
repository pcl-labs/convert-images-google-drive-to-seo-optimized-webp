"""Lightweight Google API clients implemented with urllib helpers."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from simple_http import HTTPStatusError, RequestError, SimpleClient, SimpleResponse

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

    def request(self, method: str, path: str, **kwargs) -> SimpleResponse:
        headers = self._inject_headers(kwargs.pop("headers", None))
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
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


class GoogleDriveClient:
    """Minimal Google Drive v3 client for listing/uploading/downloading files."""

    def __init__(self, token: OAuthToken):
        self.token = token
        self._metadata_session = GoogleAPISession("https://www.googleapis.com/drive/v3", token)
        self._upload_session = GoogleAPISession("https://www.googleapis.com/upload/drive/v3", token)
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

    def download_file(self, file_id: str, file_obj) -> None:
        response = self._metadata_session.request("GET", f"/files/{file_id}", params={"alt": "media"})
        file_obj.write(response.content)

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

    def files(self) -> "_GoogleDriveFilesResource":
        return self._files_resource

    def close(self) -> None:
        self._metadata_session.close()
        self._upload_session.close()


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

    def fetch_video(self, video_id: str) -> Dict[str, Any]:
        response = self._session.request(
            "GET",
            "/videos",
            params={"part": "snippet,contentDetails,status", "id": video_id},
        )
        return response.json()

    def list_captions(self, video_id: str) -> Dict[str, Any]:
        response = self._session.request(
            "GET",
            "/captions",
            params={"part": "id,snippet", "videoId": video_id},
        )
        return response.json()

    def download_caption(self, caption_id: str, *, format: str = "srt") -> str:
        response = self._session.request("GET", f"/captions/{caption_id}", params={"tfmt": format})
        if response.headers.get("content-type", "").startswith("application/json"):
            # YouTube may return JSON errors; raise a descriptive error
            raise GoogleHTTPError(response.status_code, response.text, payload=response.text)
        return response.text

    def close(self) -> None:
        self._session.close()


__all__ = [
    "GoogleAPIError",
    "GoogleHTTPError",
    "GoogleDocsClient",
    "GoogleDriveClient",
    "YouTubeClient",
    "OAuthToken",
]
