"""Google Drive upload/download helpers that use urllib under the hood."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Set, Tuple, overload, Literal

from api.simple_http import HTTPStatusError, RequestError, SimpleClient

from .google_clients import GoogleAPIError, GoogleDriveClient, OAuthToken
from .constants import DEFAULT_EXTENSIONS
from .extension_utils import normalize_extensions

# Import settings with try/except for CLI fallback compatibility
try:
    from api.config import settings
except ImportError:
    # CLI fallback: settings may not be available in CLI context
    settings = None

logger = logging.getLogger(__name__)

TOKEN_FILE = Path("token.json")
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Note: No locks needed in Cloudflare Workers - each isolate is single-threaded
# This is only used for CLI fallback token.json access


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_local_token() -> Dict[str, str]:
    """Load token from file."""
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            "token.json not found. Link your Google Drive account via the web UI or copy an access token."
        )
    try:
        return json.loads(TOKEN_FILE.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError("token.json is malformed; delete it and re-authenticate") from exc


def _refresh_local_token(token_info: Dict[str, str]) -> Dict[str, str]:
    """Refresh token with atomic file writes and proper permissions.
    
    Reads client_id/client_secret from environment/secrets (via settings) first,
    falling back to token_info for CLI compatibility.
    """
    refresh_token = token_info.get("refresh_token")
    # Prefer environment/secrets over token.json for client credentials
    if settings and settings.google_client_id and settings.google_client_secret:
        client_id = settings.google_client_id
        client_secret = settings.google_client_secret
    else:
        # CLI fallback: read from token.json if settings not available
        client_id = token_info.get("client_id")
        client_secret = token_info.get("client_secret")
    if not refresh_token or not client_id or not client_secret:
        raise RuntimeError("Cannot refresh Google token; missing refresh_token or client credentials")
    payload = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    }
    with SimpleClient(timeout=10.0) as client:
        try:
            response = client.post(TOKEN_ENDPOINT, data=payload)
            response.raise_for_status()
        except HTTPStatusError as exc:
            raise RuntimeError(f"Failed to refresh Google token: HTTP {exc.response.status_code}") from exc
        except RequestError as exc:
            raise RuntimeError("Failed to reach Google token endpoint") from exc
        data = response.json()
    
    # Google OAuth2 token endpoint returns "access_token" (not "token")
    # The fallback to "token" is kept for backward compatibility with existing token files
    access_token = data.get("access_token") or data.get("token")
    if not access_token:
        raise RuntimeError("Token refresh response missing access_token")
    token_info["token"] = access_token
    expires_in = data.get("expires_in")
    expiry = None
    try:
        if expires_in is not None:
            expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    except (ValueError, TypeError):
        expiry = None
    token_info["expiry"] = expiry.isoformat().replace("+00:00", "Z") if expiry else None
    token_info.setdefault("token_type", data.get("token_type", "Bearer"))
    
    # Atomic write with proper permissions (0o600 = owner read/write only)
    # The write is atomic via tempfile + os.replace, ensuring no corruption on interruption.
    token_json = json.dumps(token_info, indent=2)
    token_bytes = token_json.encode("utf-8")
    temp_fd, temp_path = tempfile.mkstemp(dir=TOKEN_FILE.parent, text=False)
    try:
        try:
            os.write(temp_fd, token_bytes)
            # Close the file descriptor immediately after successful write
            os.close(temp_fd)
            temp_fd = None  # Mark as closed to avoid double-close in finally
            os.chmod(temp_path, 0o600)  # Restrict to owner only
            os.replace(temp_path, TOKEN_FILE)
        except Exception:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            raise
    finally:
        # Always close the file descriptor if it's still open (e.g., if write failed)
        # Wrap in try/except to ignore double-close errors or bad descriptor values
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except (OSError, ValueError):
                # Ignore errors from closing (e.g., already closed or bad descriptor)
                pass
    
    return token_info


def get_drive_service(token: Optional[OAuthToken] = None) -> GoogleDriveClient:
    """Get Drive service with token loading and refresh.
    
    Args:
        token: Optional OAuthToken to use. If provided, uses this token directly.
               If None, falls back to loading from token.json (CLI fallback only).
    
    Returns:
        GoogleDriveClient instance
        
    Note:
        In production/worker contexts, pass a token built from DB credentials.
        token.json is only used as a CLI fallback when token is not provided.
        No locks needed in Cloudflare Workers - each isolate is single-threaded.
    """
    if token is not None:
        return GoogleDriveClient(token)
    
    # CLI fallback: load from token.json
    # No lock needed - Workers are single-threaded per isolate
    token_info = _load_local_token()
    expiry = _parse_iso(token_info.get("expiry"))
    if expiry and expiry <= datetime.now(timezone.utc) + timedelta(seconds=60):
        token_info = _refresh_local_token(token_info)
        expiry = _parse_iso(token_info.get("expiry"))
    access_token = token_info.get("access_token") or token_info.get("token")
    if not access_token:
        raise RuntimeError("token.json is missing access_token")
    token = OAuthToken(
        access_token=access_token,
        refresh_token=token_info.get("refresh_token"),
        expiry=expiry,
        token_type=token_info.get("token_type", "Bearer"),
    )
    return GoogleDriveClient(token)


def list_files_in_folder(drive_folder_id: str, service: Optional[GoogleDriveClient] = None) -> Set[str]:
    service = service or get_drive_service()
    filenames: Set[str] = set()
    page_token = None
    while True:
        try:
            response = service.list_folder_files(
                drive_folder_id,
                page_token=page_token,
                fields="nextPageToken, files(name)",
            )
        except GoogleAPIError as exc:
            logger.error("Failed to list files in folder %s: %s", drive_folder_id, exc)
            raise
        for file_entry in response.get("files", []):
            name = file_entry.get("name")
            if name:
                filenames.add(name)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return filenames


def upload_images(
    local_folder: str,
    drive_folder_id: str,
    extensions=DEFAULT_EXTENSIONS,
    fail_log_path: Optional[str] = None,
    max_retries: int = 3,
    service: Optional[GoogleDriveClient] = None,
) -> Tuple[list[str], list[str]]:
    extensions = normalize_extensions(extensions)
    service = service or get_drive_service()
    uploaded: list[str] = []
    failed: list[str] = []
    existing = list_files_in_folder(drive_folder_id, service=service)
    for fname in os.listdir(local_folder):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in extensions:
            continue
        if fname in existing:
            logger.info("[SKIP] Already exists in Drive: %s", fname)
            continue
        file_path = os.path.join(local_folder, fname)
        for attempt in range(1, max_retries + 1):
            try:
                with open(file_path, "rb") as handle:
                    service.upload_file(drive_folder_id, fname, handle)
                uploaded.append(fname)
                logger.info("Uploaded: %s", fname)
                break
            except (OSError, GoogleAPIError) as exc:
                logger.error("Failed to upload %s (attempt %s/%s): %s", fname, attempt, max_retries, exc)
                if attempt == max_retries:
                    failed.append(fname)
    if fail_log_path and failed:
        with open(fail_log_path, "a", encoding="utf-8") as flog:
            for fname in failed:
                flog.write(fname + "\n")
    return uploaded, failed


@overload
def download_images(
    drive_folder_id: str,
    local_temp_dir: str,
    extensions=DEFAULT_EXTENSIONS,
    fail_log_path: Optional[str] = None,
    max_retries: int = 3,
    return_filename_mapping: Literal[False] = False,
    service: Optional[GoogleDriveClient] = None,
) -> Tuple[list[str], list[str]]:
    ...


@overload
def download_images(
    drive_folder_id: str,
    local_temp_dir: str,
    extensions=DEFAULT_EXTENSIONS,
    fail_log_path: Optional[str] = None,
    max_retries: int = 3,
    return_filename_mapping: Literal[True] = True,
    service: Optional[GoogleDriveClient] = None,
) -> Tuple[list[str], list[str], Dict[str, str]]:
    ...


def download_images(
    drive_folder_id: str,
    local_temp_dir: str,
    extensions=DEFAULT_EXTENSIONS,
    fail_log_path: Optional[str] = None,
    max_retries: int = 3,
    return_filename_mapping: bool = False,
    service: Optional[GoogleDriveClient] = None,
) -> tuple[list[str], list[str]] | tuple[list[str], list[str], Dict[str, str]]:
    extensions = normalize_extensions(extensions)
    os.makedirs(local_temp_dir, exist_ok=True)
    service = service or get_drive_service()
    downloaded: list[str] = []
    failed: list[str] = []
    filename_map: Dict[str, str] = {}
    page_token = None
    while True:
        try:
            response = service.list_folder_files(
                drive_folder_id,
                page_token=page_token,
                fields="nextPageToken, files(id, name)",
            )
        except GoogleAPIError as exc:
            logger.error("Failed to list files for download: %s", exc)
            raise
        for file_entry in response.get("files", []):
            name = file_entry.get("name")
            file_id = file_entry.get("id")
            if not name or not file_id:
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in extensions:
                continue
            base, ext = os.path.splitext(name)
            unique_name = f"{base}_{file_id}{ext}"
            local_path = os.path.join(local_temp_dir, unique_name)
            success = False
            for attempt in range(1, max_retries + 1):
                try:
                    with open(local_path, "wb") as handle:
                        service.download_file(file_id, handle)
                    success = True
                    break
                except (OSError, GoogleAPIError) as exc:
                    logger.error(
                        "Failed to download %s (attempt %s/%s): %s",
                        file_id,
                        attempt,
                        max_retries,
                        exc,
                    )
                    if attempt == max_retries:
                        failed.append(unique_name)
            if success:
                downloaded.append(unique_name)
                if return_filename_mapping:
                    filename_map[unique_name] = file_id
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    if fail_log_path and failed:
        with open(fail_log_path, "a", encoding="utf-8") as flog:
            for fname in failed:
                flog.write(fname + "\n")
    if return_filename_mapping:
        return downloaded, failed, filename_map
    return downloaded, failed


def delete_images(drive_folder_id: str, image_ids: list[str], service: Optional[GoogleDriveClient] = None) -> Tuple[list[str], list[str]]:
    """
    Delete images from Google Drive folder.
    
    Returns:
        Tuple[list[str], list[str]]: (deleted_file_ids, failed_file_ids)
    """
    service = service or get_drive_service()
    valid_ids = set()
    deleted: list[str] = []
    failed: list[str] = []
    page_token = None
    while True:
        try:
            response = service.list_folder_files(
                drive_folder_id,
                page_token=page_token,
                fields="nextPageToken, files(id)",
            )
        except GoogleAPIError as exc:
            logger.error("Error validating folder %s: %s", drive_folder_id, exc)
            # All IDs treated as failed when validation fails
            return deleted, image_ids
        for file_entry in response.get("files", []):
            file_id = file_entry.get("id")
            if file_id:
                valid_ids.add(file_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    to_delete = [fid for fid in image_ids if fid in valid_ids]
    for file_id in to_delete:
        try:
            service.delete_file(file_id)
            logger.info("Deleted file ID: %s", file_id)
            deleted.append(file_id)
        except GoogleAPIError as exc:
            logger.error("Failed to delete %s: %s", file_id, exc)
            failed.append(file_id)
    # Add IDs that weren't in the folder to failed list
    failed.extend([fid for fid in image_ids if fid not in valid_ids])
    return deleted, failed


def get_folder_name(folder_id: str, service: Optional[GoogleDriveClient] = None) -> Optional[str]:
    service = service or get_drive_service()
    try:
        metadata = service.get_file_metadata(folder_id, fields="name")
        return metadata.get("name")
    except GoogleAPIError as exc:
        logger.error("Failed to fetch folder name: %s", exc)
        return None


def extract_folder_id_from_input(folder_input: str, service: Optional[GoogleDriveClient] = None) -> str:
    match = re.search(r"/folders/([\w-]+)", folder_input)
    candidate_id = match.group(1) if match else folder_input
    if not re.match(r"^[A-Za-z0-9_-]+$", candidate_id):
        raise ValueError("Invalid Google Drive folder link or ID format.")
    service = service or get_drive_service()
    try:
        metadata = service.get_file_metadata(candidate_id, fields="id,mimeType")
        if metadata.get("mimeType") != "application/vnd.google-apps.folder":
            raise ValueError(f"ID {candidate_id} is not a Google Drive folder.")
        return candidate_id
    except GoogleAPIError as exc:
        raise ValueError(f"Error validating Google Drive folder ID {candidate_id}: {exc}") from exc


def is_valid_drive_file_id(file_id: str) -> bool:
    # Drive IDs observed in practice vary in length; enforce only the documented
    # minimum length and character set.
    if not file_id or len(file_id) < 25:
        return False
    return bool(re.match(r"^[a-zA-Z0-9_-]+$", file_id))


__all__ = [
    "get_drive_service",
    "list_files_in_folder",
    "upload_images",
    "download_images",
    "delete_images",
    "get_folder_name",
    "extract_folder_id_from_input",
    "is_valid_drive_file_id",
]
