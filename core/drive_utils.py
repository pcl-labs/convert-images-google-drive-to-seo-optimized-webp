"""Google Drive upload/download helpers that use urllib under the hood."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from simple_http import HTTPStatusError, RequestError, SimpleClient

from .google_clients import GoogleAPIError, GoogleDriveClient, OAuthToken
from .constants import DEFAULT_EXTENSIONS
from .extension_utils import normalize_extensions

logger = logging.getLogger(__name__)

TOKEN_FILE = Path("token.json")
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_local_token() -> Dict[str, str]:
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            "token.json not found. Link your Google Drive account via the web UI or copy an access token."
        )
    try:
        return json.loads(TOKEN_FILE.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError("token.json is malformed; delete it and re-authenticate") from exc


def _refresh_local_token(token_info: Dict[str, str]) -> Dict[str, str]:
    refresh_token = token_info.get("refresh_token")
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
    token_info["token"] = data.get("access_token") or data.get("token")
    expires_in = data.get("expires_in")
    expiry = None
    try:
        if expires_in is not None:
            expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    except (ValueError, TypeError):
        expiry = None
    token_info["expiry"] = expiry.isoformat().replace("+00:00", "Z") if expiry else None
    token_info.setdefault("token_type", data.get("token_type", "Bearer"))
    TOKEN_FILE.write_text(json.dumps(token_info, indent=2))
    return token_info


def get_drive_service() -> GoogleDriveClient:
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


def download_images(
    drive_folder_id: str,
    local_temp_dir: str,
    extensions=DEFAULT_EXTENSIONS,
    fail_log_path: Optional[str] = None,
    max_retries: int = 3,
    return_filename_mapping: bool = False,
    service: Optional[GoogleDriveClient] = None,
):
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
            break
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


def delete_images(drive_folder_id: str, image_ids: list[str], service: Optional[GoogleDriveClient] = None) -> None:
    service = service or get_drive_service()
    valid_ids = set()
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
            return
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
        except GoogleAPIError as exc:
            logger.error("Failed to delete %s: %s", file_id, exc)


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
    if not file_id or len(file_id) < 25 or len(file_id) > 44:
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
