"""
Google Drive upload, download, and delete utilities.
"""

import os
import io
import json
import time
import logging
import re
from .extension_utils import normalize_extensions
from .constants import DEFAULT_EXTENSIONS, GOOGLE_DRIVE_SCOPES
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

# Supported image extensions (normalized)
IMAGE_EXTENSIONS = set(f".{e}" for e in DEFAULT_EXTENSIONS)

SCOPES = GOOGLE_DRIVE_SCOPES

def get_drive_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Validate credentials.json exists and is readable before attempting OAuth flow
            try:
                if not os.path.exists("credentials.json"):
                    raise FileNotFoundError("credentials.json not found")
                if not os.access("credentials.json", os.R_OK):
                    raise PermissionError("credentials.json is not readable")
                # Try to parse JSON to catch malformed files early
                with open("credentials.json", "r") as f:
                    json.load(f)
            except FileNotFoundError:
                error_msg = "credentials.json not found or unreadable — please provide OAuth client secrets"
                logger.error(error_msg)
                raise FileNotFoundError(error_msg)
            except (json.JSONDecodeError, PermissionError) as e:
                error_msg = f"credentials.json is malformed or unreadable — please provide valid OAuth client secrets: {e}"
                logger.error(error_msg)
                raise ValueError(error_msg)
            except Exception as e:
                error_msg = f"Error reading credentials.json — please provide valid OAuth client secrets: {e}"
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        # Only write token.json after successful authentication
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return build("drive", "v3", credentials=creds)

def list_files_in_folder(drive_folder_id, service=None, max_retries=3, retry_delay=1):
    """Return a set of filenames in the given Google Drive folder.
    
    Args:
        drive_folder_id: Google Drive folder ID
        service: Optional Drive service instance
        max_retries: Maximum number of retry attempts for transient errors (default: 3)
        retry_delay: Initial delay in seconds for exponential backoff (default: 1)
    
    Returns:
        Set of filenames in the folder
    
    Raises:
        HttpError: For non-retriable API errors
        Exception: For other unexpected errors
    """
    service = service or get_drive_service()
    page_token = None
    filenames = set()
    
    while True:
        attempt = 0
        response = None
        
        while attempt < max_retries:
            try:
                response = service.files().list(
                    q=f"'{drive_folder_id}' in parents and trashed = false",
                    spaces='drive',
                    fields='nextPageToken, files(name)',
                    pageToken=page_token
                ).execute()
                break  # Success, exit retry loop
            except HttpError as error:
                attempt += 1
                error_code = error.resp.status if hasattr(error, 'resp') else None
                
                # Check if error is retriable (5xx server errors, rate limits)
                is_retriable = (
                    error_code and (
                        error_code >= 500 or  # Server errors
                        error_code == 429 or  # Rate limit
                        error_code == 408     # Request timeout
                    )
                )
                
                if not is_retriable or attempt >= max_retries:
                    # Non-retriable error or max retries reached
                    logger.error(
                        f"Failed to list files in folder {drive_folder_id} "
                        f"(page_token: {page_token}, attempt: {attempt}/{max_retries}): {error}"
                    )
                    if not is_retriable:
                        # Exit cleanly on non-retriable errors
                        raise
                    else:
                        # Max retries reached, re-raise the last error
                        raise
                
                # Retry with exponential backoff
                delay = retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"Transient error listing files in folder {drive_folder_id} "
                    f"(page_token: {page_token}, attempt: {attempt}/{max_retries}): {error}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
                
            except Exception as e:
                # Unexpected error - log and re-raise
                logger.error(
                    f"Unexpected error listing files in folder {drive_folder_id} "
                    f"(page_token: {page_token}, attempt: {attempt}): {e}"
                )
                raise
        
        if response is None:
            # Should not reach here, but handle gracefully
            logger.error(f"Failed to get response for folder {drive_folder_id} after {max_retries} attempts")
            break
        
        # Process response
        for file in response.get('files', []):
            filenames.add(file['name'])
        
        page_token = response.get('nextPageToken', None)
        if page_token is None:
            break
    
    return filenames

def upload_images(local_folder, drive_folder_id, extensions=DEFAULT_EXTENSIONS, fail_log_path=None, max_retries=3, service=None):
    """Upload images from local_folder to Google Drive folder, skipping files that already exist."""
    extensions = normalize_extensions(extensions)
    service = service or get_drive_service()
    uploaded = []
    failed = []
    existing_files = list_files_in_folder(drive_folder_id, service=service)
    for fname in os.listdir(local_folder):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in extensions:
            continue
        if fname in existing_files:
            logger.info(f"[SKIP] Already exists in Drive: {fname}")
            continue
        file_path = os.path.join(local_folder, fname)
        for attempt in range(max_retries):
            try:
                file_metadata = {
                    'name': fname,
                    'parents': [drive_folder_id]
                }
                with open(file_path, 'rb') as f:
                    media = MediaIoBaseUpload(f, mimetype='application/octet-stream')
                    service.files().create(
                        body=file_metadata,
                        media_body=media,
                        fields='id'
                    ).execute()
                uploaded.append(fname)
                logger.info(f"Uploaded: {fname}")
                break
            except Exception as e:
                logger.error(f"Failed to upload {fname} (attempt {attempt+1}): {e}")
                if attempt == max_retries - 1:
                    failed.append(fname)
    if fail_log_path and failed:
        with open(fail_log_path, 'a') as flog:
            for fname in failed:
                flog.write(fname + '\n')
    return uploaded, failed

def download_images(drive_folder_id, local_temp_dir, extensions=DEFAULT_EXTENSIONS, fail_log_path=None, max_retries=3, return_filename_mapping=False, service=None):
    """
    Download images from Google Drive folder to local_temp_dir. Save with unique filenames if needed.
    
    Files are saved with the format: "<name>_<file_id>.<ext>" where:
    - <name> is the original filename without extension
    - <file_id> is the Google Drive file ID
    - <ext> is the file extension
    
    Args:
        drive_folder_id: Google Drive folder ID to download from
        local_temp_dir: Local directory to save downloaded files
        extensions: Set of file extensions to download (default: IMAGE_EXTENSIONS)
        fail_log_path: Optional path to log failed downloads
        max_retries: Maximum number of retry attempts per file
        return_filename_mapping: If True, return a dict mapping filename to file_id
    
    Returns:
        If return_filename_mapping is False:
            (downloaded, failed) - tuple of lists of filenames
        If return_filename_mapping is True:
            (downloaded, failed, filename_to_file_id) - tuple with mapping dict
    """
    extensions = normalize_extensions(extensions)
    os.makedirs(local_temp_dir, exist_ok=True)
    service = service or get_drive_service()
    page_token = None
    downloaded = []
    failed = []
    filename_to_file_id = {} if return_filename_mapping else None
    logger.debug(f"Extensions being used for filtering: {extensions}")
    while True:
        try:
            response = service.files().list(
                q=f"'{drive_folder_id}' in parents and trashed = false",
                spaces='drive',
                fields='nextPageToken, files(id, name, mimeType)',
                pageToken=page_token
            ).execute()
            logger.debug("Files found in folder:")
            for file in response.get('files', []):
                logger.debug(f"  - {file['name']} (mimeType: {file['mimeType']})")
            for file in response.get('files', []):
                name = file['name']
                ext = os.path.splitext(name)[1].lower()
                logger.debug(f"Checking file: {name}, extracted ext: '{ext}'")
                if ext not in extensions:
                    continue
                file_id = file['id']
                # Save with unique filename: name_fileid.ext
                name_no_ext, ext = os.path.splitext(name)
                unique_name = f"{name_no_ext}_{file_id}{ext}"
                local_path = os.path.join(local_temp_dir, unique_name)
                logger.debug(f"Attempting to download: {name} (id: {file_id}) -> {local_path}")
                for attempt in range(max_retries):
                    try:
                        request = service.files().get_media(fileId=file_id)
                        with io.FileIO(local_path, 'wb') as fh:
                            downloader = MediaIoBaseDownload(fh, request)
                            done = False
                            while not done:
                                status, done = downloader.next_chunk()
                        downloaded.append(unique_name)
                        if return_filename_mapping:
                            filename_to_file_id[unique_name] = file_id
                        logger.info(f"Downloaded: {unique_name}")
                        break
                    except Exception as e:
                        logger.error(f"Failed to download {name} (attempt {attempt+1}): {e}")
                        if attempt == max_retries - 1:
                            failed.append(unique_name)
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
        except HttpError as error:
            logger.error(f"An error occurred during download: {error}")
            break
    if fail_log_path and failed:
        with open(fail_log_path, 'a') as flog:
            for fname in failed:
                flog.write(fname + '\n')
    if return_filename_mapping:
        return downloaded, failed, filename_to_file_id
    return downloaded, failed

def delete_images(drive_folder_id, image_ids, service=None):
    """Delete images from Google Drive folder by image_ids.
    
    Validates that all image_ids belong to the specified folder before deletion.
    Files not found in the folder will be skipped with a warning.
    """
    service = service or get_drive_service()
    deleted_count = 0
    not_found_count = 0
    error_count = 0
    skipped_count = 0
    
    # Validate folder and build set of valid file IDs
    logger.info(f"Validating {len(image_ids)} file IDs against folder {drive_folder_id}...")
    valid_file_ids = set()
    page_token = None
    
    try:
        while True:
            response = service.files().list(
                q=f"'{drive_folder_id}' in parents and trashed = false",
                spaces='drive',
                fields='nextPageToken, files(id)',
                pageToken=page_token
            ).execute()
            for file in response.get('files', []):
                valid_file_ids.add(file['id'])
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
    except HttpError as error:
        logger.error(f"Error validating folder: {error}")
        error_count += len(image_ids)
        return
    
    # Filter to only validated IDs
    validated_ids = []
    for file_id in image_ids:
        if file_id in valid_file_ids:
            validated_ids.append(file_id)
        else:
            logger.warning(f"File ID {file_id} not found in folder {drive_folder_id}, skipping")
            skipped_count += 1
    
    logger.info(f"Attempting to delete {len(validated_ids)} validated files...")
    
    for file_id in validated_ids:
        try:
            service.files().delete(fileId=file_id).execute()
            logger.info(f"Deleted file ID: {file_id}")
            deleted_count += 1
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(f"File not found (already deleted?): {file_id}")
                not_found_count += 1
            else:
                logger.error(f"Failed to delete file ID {file_id}: {e}")
                error_count += 1
        except Exception as e:
            logger.error(f"Failed to delete file ID {file_id}: {e}")
            error_count += 1
    
    # Print summary
    logger.info("Deletion summary:")
    logger.info(f"  Successfully deleted: {deleted_count}")
    logger.info(f"  Not found (already deleted): {not_found_count}")
    logger.info(f"  Skipped (not in folder): {skipped_count}")
    logger.info(f"  Errors: {error_count}")

def get_folder_name(folder_id, service=None):
    """Fetch the name of a Google Drive folder by its ID."""
    service = service or get_drive_service()
    try:
        folder = service.files().get(fileId=folder_id, fields='name').execute()
        return folder['name']
    except Exception as e:
        logger.error(f"Failed to get folder name for ID {folder_id}: {e}")
        return None 

def extract_folder_id_from_input(folder_input: str, service=None) -> str:
    """
    Extract folder ID from share link or return as-is if already an ID.
    Validates the folder ID using the Drive API to ensure it exists and is accessible.
    """
    match = re.search(r"/folders/([\w-]+)", folder_input)
    if match:
        candidate_id = match.group(1)
    else:
        if not re.match(r"^[A-Za-z0-9_-]+$", folder_input):
            raise ValueError("Invalid Google Drive folder link or ID format.")
        candidate_id = folder_input

    service = service or get_drive_service()
    try:
        folder = service.files().get(
            fileId=candidate_id,
            fields='id, mimeType'
        ).execute()
        if folder.get('mimeType') != 'application/vnd.google-apps.folder':
            raise ValueError(f"ID {candidate_id} is not a Google Drive folder.")
        return candidate_id
    except HttpError as e:
        if e.resp.status == 404:
            raise ValueError(f"Google Drive folder with ID {candidate_id} not found or not accessible.")
        elif e.resp.status == 403:
            raise ValueError(f"Access denied to Google Drive folder with ID {candidate_id}.")
        else:
            raise ValueError(f"Error validating Google Drive folder ID {candidate_id}: {e}")
    except Exception as e:
        raise ValueError(f"Error validating Google Drive folder ID {candidate_id}: {e}")

def is_valid_drive_file_id(file_id: str) -> bool:
    """Check if a string looks like a valid Google Drive file ID.
    Uses a conservative length range (25–44) and allowed chars.
    """
    if not file_id or len(file_id) < 25 or len(file_id) > 44:
        return False
    if not re.match(r"^[a-zA-Z0-9_-]+$", file_id):
        return False
    return True