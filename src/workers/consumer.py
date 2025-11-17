"""
Cloudflare Worker consumer for processing image optimization jobs from the queue.

Filename Format Requirement:
    Downloaded images are saved with the format: "<name>_<file_id>.<ext>"
    where:
    - <name> is the original filename without extension
    - <file_id> is the Google Drive file ID (minimum 20 characters, alphanumeric with underscores/hyphens)
    - <ext> is the file extension
    - The separator between name and file_id is configurable via FILENAME_ID_SEPARATOR constant
    
    Example: "my-image_1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r9s0t.jpg"
    
    This format is required for extracting file IDs during cleanup operations.
    Files that don't match this pattern will be logged with a warning and skipped
    during file ID extraction.
"""

import os
import sys
from pathlib import Path

# Add project root to Python path for imports
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import re
import json
from typing import Dict, Any, Optional, List
import asyncio
import functools
import textwrap
from datetime import datetime, timezone, timedelta
import uuid

from src.workers.core.drive_utils import (
    download_images,
    upload_images,
    delete_images,
    get_folder_name,
    extract_folder_id_from_input,
    is_valid_drive_file_id,
)
from src.workers.core.image_processor import process_image
from src.workers.api.database import (
    Database,
    update_job_status,
    update_document,
    set_job_output,
    record_usage_event,
    get_document,
    create_document_version,
    get_job,
    update_job_retry_state,
    create_job_extended,
    record_pipeline_event,
)
from src.workers.api.config import settings
from src.workers.api.cloudflare_queue import QueueProducer
from src.workers.api.google_oauth import build_youtube_service_for_user, build_docs_service_for_user, build_drive_service_for_user
from src.workers.api.drive_workspace import DriveWorkspaceSyncService, link_document_drive_workspace
from src.workers.api.drive_docs import sync_drive_doc_for_document
from src.workers.api.drive_watch import ensure_drive_watch, watches_due_for_renewal
from src.workers.core.ai_modules import (
    generate_outline,
    organize_chapters,
    compose_blog,
    default_title_from_outline,
    generate_seo_metadata,
    generate_image_prompts,
    markdown_to_html,
)
from src.workers.api.notifications import notify_job
from src.workers.api.app_logging import setup_logging, get_logger
from src.workers.core.filename_utils import FILENAME_ID_SEPARATOR, sanitize_folder_name, parse_download_name, make_output_dir_name
from src.workers.core.constants import TEMP_DIR, FAIL_LOG_PATH
from src.workers.core.extension_utils import normalize_extensions, detect_extensions_in_dir
from src.workers.core.google_async import execute_google_request
from src.workers.core.google_docs_text import google_doc_to_text, text_to_html
from src.workers.api.youtube_ingest import ingest_youtube_document
from src.workers.core.youtube_captions import YouTubeCaptionsError

# Set up logging
logger = setup_logging(level="INFO", use_json=True)
app_logger = get_logger(__name__)


def _retry_delay_seconds(attempt_count: int) -> int:
    """Simple exponential backoff with cap to avoid hot-looping."""
    attempt = max(attempt_count, 1)
    base_delay = 5
    delay = base_delay * (2 ** (attempt - 1))
    return min(delay, 300)


async def _handle_job_failure(
    db: Database,
    job_row: Optional[Dict[str, Any]],
    error_message: str,
    message: Dict[str, Any],
    queue_producer: Optional[QueueProducer],
) -> None:
    if not job_row:
        app_logger.error("Cannot handle retry for unknown job", extra={"job_id": message.get("job_id")})
        return

    job_id = job_row.get("job_id")
    user_id = job_row.get("user_id")
    previous_attempts = 0
    try:
        previous_attempts = int(job_row.get("attempt_count") or 0)
    except Exception:
        previous_attempts = 0
    new_attempt = previous_attempts + 1
    # Interpret max_job_retries as TOTAL allowed attempts (including the first).
    # None or values <1 are treated as "at least one attempt".
    try:
        _mr = settings.max_job_retries
        if _mr is None:
            max_attempts = 1
        else:
            max_attempts = max(1, int(_mr))
    except (TypeError, ValueError):
        max_attempts = 1

    if new_attempt >= max_attempts:
        await update_job_retry_state(db, job_id, new_attempt, None, error_message)
        await update_job_status(db, job_id, "failed", error=error_message)
        try:
            await notify_job(
                db,
                user_id=user_id,
                job_id=job_id,
                level="error",
                text=f"Job {job_id} failed after {new_attempt} attempts",
            )
        except Exception:
            pass
        if queue_producer:
            try:
                await queue_producer.send_to_dlq(job_id, error_message, message)
            except Exception:
                app_logger.warning("Failed to send job to DLQ", exc_info=True, extra={"job_id": job_id})
        return

    retry_delay = _retry_delay_seconds(new_attempt)
    next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_delay)).isoformat()
    await update_job_retry_state(db, job_id, new_attempt, next_attempt_at, error_message)
    retry_progress = make_progress(
        "retry_waiting",
        recent_logs=[f"retry in {retry_delay}s (attempt {new_attempt}/{max_attempts})"],
    )
    await update_job_status(
        db,
        job_id,
        "pending",
        progress=retry_progress,
        error=error_message,
    )
    if not settings.use_inline_queue and queue_producer is not None:
        try:
            await queue_producer.send_generic(message)
        except Exception:
            app_logger.exception(
                "Failed to re-enqueue job for retry",
                extra={"job_id": job_id, "attempt": new_attempt},
            )



def make_progress(
    stage: str,
    downloaded: int = 0,
    optimized: int = 0,
    skipped: int = 0,
    uploaded: int = 0,
    deleted: int = 0,
    download_failed: int = 0,
    upload_failed: int = 0,
    processing_failed: int = 0,
    recent_logs: Optional[list[str]] = None,
):
    return {
        "stage": stage,
        "downloaded": downloaded,
        "optimized": optimized,
        "skipped": skipped,
        "uploaded": uploaded,
        "deleted": deleted,
        "download_failed": download_failed,
        "upload_failed": upload_failed,
        "processing_failed": processing_failed,
        "recent_logs": recent_logs or [],
    }


async def _safe_pipeline_event(
    db: Database,
    user_id: str,
    job_id: Optional[str],
    *,
    event_type: str,
    stage: str,
    status: str,
    message: str,
    data: Optional[Dict[str, Any]] = None,
    notify_level: Optional[str] = None,
    notify_text: Optional[str] = None,
    notify_context: Optional[Dict[str, Any]] = None,
) -> None:
    if not job_id:
        return
    try:
        await record_pipeline_event(
            db,
            user_id,
            job_id,
            event_type=event_type,
            stage=stage,
            status=status,
            message=message,
            data=data or {},
            notify_level=notify_level,
            notify_text=notify_text,
            notify_context=notify_context,
        )
    except Exception:
        app_logger.debug(
            "pipeline_event_emit_failed",
            exc_info=True,
            extra={"job_id": job_id, "stage": stage, "event_type": event_type},
        )

def extract_file_id_from_filename(
    filename: str,
    separator: str = FILENAME_ID_SEPARATOR
) -> Optional[str]:
    """
    Extract Google Drive file ID from a downloaded filename.
    
    Expected format: "<name><separator><file_id>.<ext>"
    
    Args:
        filename: The filename to extract the file ID from
        separator: The separator character/string between name and file_id (default: '_')
    
    Returns:
        The extracted file ID if valid, None otherwise
    
    Logs:
        Warning if filename doesn't match expected pattern or extracted ID is invalid
    """
    try:
        parsed = parse_download_name(filename, sep=separator)
        if not parsed:
            app_logger.warning(
                f"Filename does not match expected pattern '<name>{separator}<file_id>.<ext>': {filename}",
                extra={"filename": filename, "reason": "pattern_mismatch", "separator": separator}
            )
            return None
        _, file_id, _ = parsed
        if not is_valid_drive_file_id(file_id):
            app_logger.warning(
                f"Extracted file ID from filename is invalid: '{file_id}' (from {filename})",
                extra={
                    "filename": filename,
                    "extracted_id": file_id,
                    "reason": "invalid_file_id_format"
                }
            )
            return None
        return file_id
    except Exception as e:
        app_logger.warning(
            f"Error extracting file ID from filename '{filename}': {e}",
            extra={"filename": filename, "reason": "extraction_error", "error": str(e)},
            exc_info=True
        )
        return None


def _parse_document_metadata(raw: Dict[str, Any]) -> Dict[str, Any]:
    metadata = raw.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if metadata is None:
        metadata = {}
    return metadata


def _json_dict_field(value: Any, default: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return dict(default)
    if isinstance(value, dict):
        return value
    return dict(default)


async def _resolve_drive_folder(db: Database, document_id: str, user_id: str) -> str:
    doc = await get_document(db, document_id, user_id=user_id)
    if not doc:
        raise ValueError("Document not found for optimize job")
    metadata = _parse_document_metadata(doc)
    source_type = doc.get("source_type")
    if source_type not in {"drive", "drive_folder"}:
        raise ValueError("Document is not associated with a Drive folder")
    folder_id = (
        doc.get("drive_folder_id")
        or doc.get("source_ref")
        or metadata.get("drive_folder_id")
        or (metadata.get("drive") or {}).get("folder_id")
    )
    if not folder_id:
        raise ValueError("Drive folder reference missing on document")
    return folder_id


async def process_optimization_job(
    db: Database,
    job_id: str,
    user_id: str,
    drive_folder: str,
    extensions: list,
    overwrite: bool,
    skip_existing: bool,
    cleanup_originals: bool,
    max_retries: int
):
    """
    Process an image optimization job.
    
    Downloads images from Google Drive, optimizes them, uploads optimized versions,
    and optionally deletes originals. Downloaded files are saved with the format:
    "<name>_<file_id>.<ext>" to enable file ID extraction during cleanup operations.
    
    See module docstring for detailed filename format requirements.
    """
    try:
        # Local recent logs buffer (keeps last 20)
        recent_logs: list[str] = []
        MAX_RECENT = 50

        # Basic sanitizer to reduce PII/secret leakage in progress logs
        SENSITIVE_PATTERNS = [
            # Bearer/API tokens
            re.compile(r"(bearer\s+[A-Za-z0-9\-_.=:+/]{10,})", re.IGNORECASE),
            re.compile(r"(api[_-]?key\s*[:=]\s*[A-Za-z0-9\-_.=:+/]{10,})", re.IGNORECASE),
            re.compile(r"(token\s*[:=]\s*[A-Za-z0-9\-_.=:+/]{10,})", re.IGNORECASE),
            # Emails
            re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}") ,
            # IPv4
            re.compile(r"\b(?:(?:2[0-5]{2}|1?\d?\d)\.){3}(?:2[0-5]{2}|1?\d?\d)\b"),
            # File paths (basic)
            re.compile(r"(/[^\s]+)+"),
            re.compile(r"([A-Za-z]:\\[^\s]+)"),
            # URLs with potential creds
            re.compile(r"https?://[^\s]+"),
        ]

        def sanitize_log_entry(msg: str) -> str:
            try:
                s = str(msg)
                for pat in SENSITIVE_PATTERNS:
                    s = pat.sub("[REDACTED]", s)
                # trim overly long messages
                if len(s) > 300:
                    s = s[:297] + "..."
                return s
            except Exception:
                return "[log]"

        def log_step(msg: str):
            safe = sanitize_log_entry(msg)
            recent_logs.append(safe)
            if len(recent_logs) > MAX_RECENT:
                del recent_logs[0:len(recent_logs) - MAX_RECENT]

        # Update status to processing
        log_step("Initializing: extracting folder ID")
        app_logger.info(
            f"Job {job_id} status transition: pending -> processing",
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "event": "job.status_transition",
                "old_status": "pending",
                "new_status": "processing",
                "stage": "extracting_folder_id"
            }
        )
        await update_job_status(db, job_id, "processing", progress=make_progress("extracting_folder_id", recent_logs=recent_logs))
        
        # Build per-user Drive service
        service = await build_drive_service_for_user(db, user_id)

        # Extract folder ID (validate access using user's Drive service)
        folder_id = extract_folder_id_from_input(drive_folder, service=service)
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="optimize_drive",
            stage="images.metadata",
            status="completed",
            message="Drive folder resolved",
            data={"folder_id": folder_id},
        )
        
        # Get folder name for SEO prefix
        folder_name = get_folder_name(folder_id, service=service) or "optimized"
        folder_name_clean = sanitize_folder_name(folder_name)
        
        # Set up directories
        output_dir = make_output_dir_name(folder_name)
        temp_dir = TEMP_DIR
        os.makedirs(temp_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        
        # Convert extensions to set with dots
        extensions_set = normalize_extensions(extensions)
        
        # Download images
        log_step("Download started")
        app_logger.info(
            f"Job {job_id} progress: stage -> downloading",
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "event": "job.progress",
                "status": "processing",
                "stage": "downloading"
            }
        )
        await update_job_status(db, job_id, "processing", progress=make_progress("downloading", recent_logs=recent_logs))
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="optimize_drive",
            stage="images.download",
            status="running",
            message="Downloading Drive images",
            data={"folder_id": folder_id, "extensions": list(extensions_set)},
        )
        
        downloaded, failed_downloads, filename_to_file_id = await asyncio.to_thread(
            download_images,
            folder_id,
            temp_dir,
            extensions_set,
            FAIL_LOG_PATH,
            max_retries,
            True,
            service
        )
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="optimize_drive",
            stage="images.download",
            status="completed",
            message="Images downloaded",
            data={"folder_id": folder_id, "downloaded": len(downloaded), "failed": len(failed_downloads)},
        )
        
        log_step(f"Download finished: {len(downloaded)} ok, {len(failed_downloads)} failed")
        await update_job_status(db, job_id, "processing", progress=make_progress(
            "downloading",
            downloaded=len(downloaded),
            download_failed=len(failed_downloads),
            recent_logs=recent_logs,
        ))
        
        # Optimize images
        log_step("Optimization started")
        app_logger.info(
            f"Job {job_id} progress: stage -> optimizing",
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "event": "job.progress",
                "status": "processing",
                "stage": "optimizing",
                "downloaded": len(downloaded),
                "download_failed": len(failed_downloads)
            }
        )
        await update_job_status(db, job_id, "processing", progress=make_progress(
            "optimizing",
            downloaded=len(downloaded),
            download_failed=len(failed_downloads),
            recent_logs=recent_logs,
        ))
        
        optimized = []
        skipped = []
        failed_processing = []
        PROGRESS_UPDATE_INTERVAL = 10
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="optimize_drive",
            stage="images.optimize",
            status="running",
            message="Optimizing images",
            data={"folder_id": folder_id},
        )
        
        for idx, fname in enumerate(downloaded, 1):
            try:
                input_path = os.path.join(temp_dir, fname)
                out_path, status = await asyncio.to_thread(
                    functools.partial(
                        process_image,
                        input_path,
                        output_dir,
                        overwrite=overwrite,
                        skip_existing=skip_existing,
                        versioned=False,
                        seo_prefix=folder_name_clean,
                    )
                )
                if status == 'skipped':
                    skipped.append(fname)
                else:
                    optimized.append(fname)
            except Exception as e:
                # Log error with filename and details
                app_logger.error(
                    f"Failed to process image {fname}: {e}",
                    exc_info=True,
                    extra={"filename": fname, "error": str(e)}
                )
                failed_processing.append(fname)
            
            # Update progress every N files or on last file
            if idx % PROGRESS_UPDATE_INTERVAL == 0 or idx == len(downloaded):
                log_step(f"Optimization progress: {len(optimized)} optimized, {len(skipped)} skipped, {len(failed_processing)} failed")
                await update_job_status(db, job_id, "processing", progress=make_progress(
                    "optimizing",
                    downloaded=len(downloaded),
                    optimized=len(optimized),
                    skipped=len(skipped),
                    download_failed=len(failed_downloads),
                    processing_failed=len(failed_processing),
                    recent_logs=recent_logs,
                ))
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="optimize_drive",
            stage="images.optimize",
            status="completed",
            message="Optimization phase complete",
            data={
                "folder_id": folder_id,
                "optimized": len(optimized),
                "skipped": len(skipped),
                "processing_failed": len(failed_processing),
            },
        )
        
        # Upload optimized images
        log_step("Upload started")
        app_logger.info(
            f"Job {job_id} progress: stage -> uploading",
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "event": "job.progress",
                "status": "processing",
                "stage": "uploading",
                "downloaded": len(downloaded),
                "optimized": len(optimized),
                "skipped": len(skipped),
                "download_failed": len(failed_downloads),
                "processing_failed": len(failed_processing)
            }
        )
        await update_job_status(db, job_id, "processing", progress=make_progress(
            "uploading",
            downloaded=len(downloaded),
            optimized=len(optimized),
            skipped=len(skipped),
            download_failed=len(failed_downloads),
            processing_failed=len(failed_processing),
            recent_logs=recent_logs,
        ))
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="optimize_drive",
            stage="images.upload",
            status="running",
            message="Uploading optimized assets to Drive",
            data={
                "folder_id": folder_id,
                "optimized": len(optimized),
                "skipped": len(skipped),
            },
        )
        
        # Detect actual extensions in output directory, with fallback defaults
        upload_extensions = detect_extensions_in_dir(output_dir)
        app_logger.info(
            f"Detected extensions for upload: {upload_extensions}",
            extra={"output_dir": output_dir, "extensions": upload_extensions}
        )
        
        uploaded, failed_uploads = await asyncio.to_thread(
            upload_images,
            output_dir,
            folder_id,
            upload_extensions,
            FAIL_LOG_PATH,
            max_retries,
            service
        )
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="optimize_drive",
            stage="images.upload",
            status="completed",
            message="Uploaded optimized images",
            data={
                "folder_id": folder_id,
                "uploaded": len(uploaded),
                "upload_failed": len(failed_uploads),
                "optimized": len(optimized),
            },
        )
        
        log_step(f"Upload finished: {len(uploaded)} ok, {len(failed_uploads)} failed")
        await update_job_status(db, job_id, "processing", progress=make_progress(
            "uploading",
            downloaded=len(downloaded),
            optimized=len(optimized),
            skipped=len(skipped),
            uploaded=len(uploaded),
            download_failed=len(failed_downloads),
            upload_failed=len(failed_uploads),
            processing_failed=len(failed_processing),
            recent_logs=recent_logs,
        ))
        
        # Delete originals if requested
        deleted_count = 0
        if cleanup_originals:
            log_step("Cleanup started")
            await update_job_status(db, job_id, "processing", progress=make_progress(
                "cleaning_up",
                downloaded=len(downloaded),
                optimized=len(optimized),
                skipped=len(skipped),
                uploaded=len(uploaded),
                download_failed=len(failed_downloads),
                upload_failed=len(failed_uploads),
                processing_failed=len(failed_processing),
                recent_logs=recent_logs,
            ))
            
            original_file_ids = []
            # Use mapping if available, otherwise extract from filenames
            for fname in downloaded:
                file_id = None
                if filename_to_file_id and fname in filename_to_file_id:
                    # Use direct mapping from download_images
                    file_id = filename_to_file_id[fname]
                    if not is_valid_drive_file_id(file_id):
                        app_logger.warning(
                            f"File ID from mapping is invalid: '{file_id}' (from {fname})",
                            extra={
                                "filename": fname,
                                "file_id": file_id,
                                "reason": "invalid_file_id_from_mapping"
                            }
                        )
                        file_id = None
                else:
                    # Fallback to extracting from filename
                    file_id = extract_file_id_from_filename(fname, separator=FILENAME_ID_SEPARATOR)
                
                if file_id:
                    original_file_ids.append(file_id)
            
            if original_file_ids:
                deleted_ids, failed_ids = await asyncio.to_thread(delete_images, folder_id, original_file_ids, service)
                deleted_count = len(deleted_ids)
                failed_count = len(failed_ids)
                if failed_count > 0:
                    log_step(f"Cleanup finished: deleted {deleted_count} originals, {failed_count} failed")
                else:
                    log_step(f"Cleanup finished: deleted {deleted_count} originals")
            else:
                app_logger.warning(
                    f"No valid file IDs extracted for cleanup from {len(downloaded)} downloaded files",
                    extra={"downloaded_count": len(downloaded)}
                )
        
        # Clean up local directories
        import shutil
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception as e:
            app_logger.warning(
                f"Failed to cleanup temp directory: {temp_dir}",
                extra={"path": temp_dir, "exception": str(e), "exception_type": type(e).__name__}
            )
        try:
            if os.path.exists(output_dir):
                shutil.rmtree(output_dir)
        except Exception as e:
            app_logger.warning(
                f"Failed to cleanup output directory: {output_dir}",
                extra={"path": output_dir, "exception": str(e), "exception_type": type(e).__name__}
            )
        
        # Mark as completed
        log_step("Completed successfully")
        await update_job_status(db, job_id, "completed", progress=make_progress(
            "completed",
            downloaded=len(downloaded),
            optimized=len(optimized),
            skipped=len(skipped),
            uploaded=len(uploaded),
            deleted=deleted_count,
            download_failed=len(failed_downloads),
            upload_failed=len(failed_uploads),
            processing_failed=len(failed_processing),
            recent_logs=recent_logs,
        ))

        app_logger.info(
            f"Job {job_id} completed successfully",
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "event": "job.status_transition",
                "old_status": "processing",
                "new_status": "completed",
                "stage": "completed",
                "stats": {
                    "downloaded": len(downloaded),
                    "optimized": len(optimized),
                    "skipped": len(skipped),
                    "uploaded": len(uploaded),
                    "deleted": deleted_count,
                    "download_failed": len(failed_downloads),
                    "upload_failed": len(failed_uploads),
                    "processing_failed": len(failed_processing)
                }
            }
        )
        # Create success notification
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="success", text=f"Job {job_id} completed")
        except Exception:
            pass
        
    except Exception as e:
        app_logger.error(
            f"Job {job_id} failed: {e}",
            exc_info=True,
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "event": "job.status_transition",
                "old_status": "processing",
                "new_status": "failed",
                "error": str(e)
            }
        )
        await update_job_status(
            db,
            job_id,
            "failed",
            error=str(e)
        )
        # Create failure notification
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="error", text=f"Job {job_id} failed")
        except Exception:
            pass
        raise


async def process_ingest_youtube_job(
    db: Database,
    job_id: str,
    user_id: str,
    document_id: str,
    youtube_video_id: str,
    job_payload: Optional[Dict[str, Any]] = None,
):
    """Fetch transcript, merge stored metadata, persist document contents, and record usage."""
    try:
        await update_job_status(db, job_id, "processing", progress=make_progress("ingesting_youtube"))
        await record_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="ingest_youtube",
            stage="job.start",
            status="running",
            message="YouTube ingest job started",
            data={"video_id": youtube_video_id},
        )

        job_payload = job_payload or {}

        def _normalize_duration(value: Any) -> Optional[int]:
            try:
                if value is None:
                    return None
                return int(float(value))
            except (TypeError, ValueError):
                return None

        payload_metadata = job_payload.get("metadata") or {}
        payload_frontmatter = job_payload.get("frontmatter") or {}
        payload_duration = _normalize_duration(job_payload.get("duration_s") or payload_metadata.get("duration_seconds"))

        try:
            result = await ingest_youtube_document(
                db,
                job_id,
                user_id,
                document_id,
                youtube_video_id,
                payload_metadata,
                payload_frontmatter,
                payload_duration,
            )
        except YouTubeCaptionsError as exc:
            await update_job_status(db, job_id, "failed", error=str(exc))
            try:
                await notify_job(db, user_id=user_id, job_id=job_id, level="error", text=f"YouTube ingestion failed: {exc}")
            except Exception:
                pass
            return
        except ValueError as exc:
            await update_job_status(db, job_id, "failed", error=str(exc))
            try:
                await notify_job(db, user_id=user_id, job_id=job_id, level="error", text=f"YouTube ingestion failed: {exc}")
            except Exception:
                pass
            return

        await set_job_output(db, job_id, result["job_output"])
        title_hint = (
            payload_frontmatter.get("title")
            or payload_metadata.get("title")
            or youtube_video_id
        )

        if settings.enable_drive_pipeline:
            await _safe_pipeline_event(
                db,
                user_id,
                job_id,
                event_type="ingest_youtube",
                stage="drive.workspace.ensure",
                status="running",
                message="Linking document to Drive workspace",
                data={"document_id": document_id},
            )
            drive_block = None
            try:
                drive_block = await link_document_drive_workspace(
                    db,
                    user_id=user_id,
                    document_id=document_id,
                    document_name=title_hint,
                    metadata=result.get("document_metadata"),
                    job_id=job_id,
                    event_type="ingest_youtube",
                )
                try:
                    await sync_drive_doc_for_document(
                        db,
                        user_id,
                        document_id,
                        {"metadata": {"drive_stage": "transcript"}},
                    )
                except Exception as exc:
                    app_logger.warning(
                        "drive_doc_seed_failed",
                        exc_info=True,
                        extra={"job_id": job_id, "document_id": document_id, "error": str(exc)},
                    )
            except Exception as exc:
                app_logger.warning(
                    "drive_workspace_link_failed",
                    exc_info=True,
                    extra={"job_id": job_id, "document_id": document_id, "error": str(exc)},
                )
                await _safe_pipeline_event(
                    db,
                    user_id,
                    job_id,
                    event_type="ingest_youtube",
                    stage="drive.workspace.link",
                    status="error",
                    message=f"Drive workspace link failed: {exc}",
                    data={"document_id": document_id},
                )
        else:
            await _safe_pipeline_event(
                db,
                user_id,
                job_id,
                event_type="ingest_youtube",
                stage="drive.workspace.link",
                status="skipped",
                message="Drive workspace linking disabled",
                data={"document_id": document_id},
            )

        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="ingest_youtube",
            stage="job.persist",
            status="completed",
            message="YouTube transcription persisted",
            data={"document_id": document_id},
            notify_level="success",
            notify_text=f"YouTube transcript saved",
            notify_context={"document_id": document_id},
        )
        await update_job_status(db, job_id, "completed", progress=make_progress("completed"))
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="success", text=f"Ingested YouTube {youtube_video_id}")
        except Exception:
            pass
        return
    except Exception as e:
        await update_job_status(db, job_id, "failed", error=str(e))
        try:
            await record_pipeline_event(
                db,
                user_id,
                job_id,
                event_type="ingest_youtube",
                stage="job.failed",
                status="error",
                message=str(e),
                data={"video_id": youtube_video_id},
            )
        except Exception:
            pass
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="error", text=f"YouTube ingestion failed")
        except Exception:
            pass
        raise


async def process_ingest_drive_job(
    db: Database,
    job_id: str,
    user_id: str,
    document_id: str,
    drive_file_id: str,
    previous_revision: Optional[str] = None,
):
    try:
        await update_job_status(db, job_id, "processing", progress=make_progress("drive_ingest.fetching"))
        document = await get_document(db, document_id, user_id=user_id)
        if not document:
            raise ValueError("Document not found for Drive ingest")
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="ingest_drive",
            stage="drive.sync.fetch",
            status="running",
            message="Fetching Google Doc contents",
            data={"document_id": document_id, "drive_file_id": drive_file_id},
        )
        docs_service = await build_docs_service_for_user(db, user_id)
        drive_service = await build_drive_service_for_user(db, user_id)
        doc_payload = await execute_google_request(docs_service.documents().get(documentId=drive_file_id))
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="ingest_drive",
            stage="drive.sync.fetch",
            status="completed",
            message="Fetched Google Doc",
            data={"document_id": document_id, "drive_file_id": drive_file_id},
        )
        text = google_doc_to_text(doc_payload)
        drive_meta = await execute_google_request(
            drive_service.files().get(
                fileId=drive_file_id,
                fields='id, name, headRevisionId, webViewLink, modifiedTime'
            )
        )
        revision_id = drive_meta.get("headRevisionId") or previous_revision
        now_iso = datetime.now(timezone.utc).isoformat()
        metadata = _parse_document_metadata(document)
        drive_block = metadata.get("drive") if isinstance(metadata, dict) else {}
        if not isinstance(drive_block, dict):
            drive_block = {}
        drive_block.update(
            {
                "file_id": drive_file_id,
                "revision_id": revision_id,
                "name": drive_meta.get("name"),
                "web_view_link": drive_meta.get("webViewLink"),
                "last_ingested_revision": revision_id,
                "last_ingested_at": now_iso,
                "external_edit_detected": False,
                "modified_time": drive_meta.get("modifiedTime"),
            }
        )
        metadata["drive"] = drive_block
        frontmatter = _json_dict_field(document.get("frontmatter"), {})
        if doc_payload.get("title") and not frontmatter.get("title"):
            frontmatter["title"] = doc_payload.get("title")
        html_body = text_to_html(text)
        version_row = await create_document_version(
            db,
            document_id=document_id,
            user_id=user_id,
            content_format="drive_doc",
            frontmatter=frontmatter,
            body_mdx=text,
            body_html=html_body,
            outline=[],
            chapters=[],
            sections=[],
            assets={},
        )
        version_id = version_row.get("version_id")
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="ingest_drive",
            stage="drive.sync.persist",
            status="running",
            message="Persisting Drive revision locally",
            data={"document_id": document_id, "version_id": version_id},
        )
        await update_document(
            db,
            document_id,
            {
                "raw_text": text,
                "metadata": metadata,
                "frontmatter": frontmatter,
                "content_format": "drive_doc",
                "latest_version_id": version_id,
                "drive_file_id": drive_file_id,
                "drive_revision_id": revision_id,
            },
        )
        job_output = {
            "document_id": document_id,
            "drive_file_id": drive_file_id,
            "drive_revision_id": revision_id,
            "chars": len(text),
            "version_id": version_id,
        }
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="ingest_drive",
            stage="drive.sync.persist",
            status="completed",
            message="Drive document synced",
            data={"document_id": document_id, "drive_revision_id": revision_id},
            notify_level="success",
            notify_text=f"Drive document synced ({drive_file_id[:8]}…)",
            notify_context={
                "document_id": document_id,
                "drive_file_id": drive_file_id,
            },
        )
        await set_job_output(db, job_id, job_output)
        await update_job_status(db, job_id, "completed", progress=make_progress("completed"))
        try:
            await notify_job(
                db,
                user_id=user_id,
                job_id=job_id,
                level="success",
                text=f"Drive document synced ({drive_file_id[:8]}…)",
            )
        except Exception:
            pass
    except Exception as exc:
        await update_job_status(db, job_id, "failed", error=str(exc))
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="Drive ingest failed")
        except Exception:
            pass
        raise


async def process_ingest_text_job(
    db: Database,
    job_id: str,
    user_id: str,
    document_id: str,
):
    """Phase 1 stub: text is already stored in document; mark job completed."""
    try:
        await update_job_status(db, job_id, "processing", progress=make_progress("ingesting_text"))
        await update_job_status(db, job_id, "completed", progress=make_progress("completed"))
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="success", text=f"Text ingested")
        except Exception:
            pass
    except Exception as e:
        await update_job_status(db, job_id, "failed", error=str(e))
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="error", text=f"Text ingestion failed")
        except Exception:
            pass
        raise


async def _enqueue_drive_ingest_followup(
    db: Database,
    user_id: str,
    document_id: str,
    drive_file_id: str,
    current_revision: Optional[str],
    queue_producer: Optional[QueueProducer],
):
    new_job_id = str(uuid.uuid4())
    await create_job_extended(
        db,
        new_job_id,
        user_id,
        job_type="ingest_drive",
        document_id=document_id,
        payload={"drive_file_id": drive_file_id, "drive_revision_id": current_revision, "trigger": "drive_change_poll"},
    )
    message = {
        "job_id": new_job_id,
        "user_id": user_id,
        "job_type": "ingest_drive",
        "document_id": document_id,
        "drive_file_id": drive_file_id,
        "drive_revision_id": current_revision,
    }
    if queue_producer:
        await queue_producer.send_generic(message)
    else:
        await process_ingest_drive_job(db, new_job_id, user_id, document_id, drive_file_id, current_revision)


async def process_drive_change_poll_job(
    db: Database,
    job_id: str,
    user_id: str,
    document_ids: Optional[List[str]] = None,
    queue_producer: Optional[QueueProducer] = None,
):
    try:
        await update_job_status(db, job_id, "processing", progress=make_progress("drive_poll.fetching"))

        async def _handle_change(document: Dict[str, Any], revision_id: str, drive_meta: Dict[str, Any]) -> None:
            await _enqueue_drive_ingest_followup(
                db,
                user_id,
                document.get("document_id"),
                document.get("drive_file_id"),
                revision_id,
                queue_producer,
            )

        sync_service = DriveWorkspaceSyncService(db, user_id, job_id=job_id, event_type="drive_sync")
        result = await sync_service.scan_for_changes(document_ids=document_ids, on_change=_handle_change)
        await set_job_output(db, job_id, result)
        await update_job_status(db, job_id, "completed", progress=make_progress("completed"))
    except Exception as exc:
        await update_job_status(db, job_id, "failed", error=str(exc))
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="Drive change poll failed")
        except Exception:
            pass
        raise


async def process_drive_watch_renewal_job(
    db: Database,
    job_id: str,
    user_id: str,
) -> None:
    try:
        await update_job_status(db, job_id, "processing", progress=make_progress("drive_watch_renewal"))
        window_minutes = max(int(getattr(settings, "drive_watch_renewal_window_minutes", 60) or 60), 1)
        candidates = await watches_due_for_renewal(db, window_minutes, user_id=user_id)
        renewed: List[str] = []
        checked_count = 0
        for watch in candidates:
            checked_count += 1
            document_id = watch.get("document_id")
            drive_file_id = watch.get("drive_file_id")
            if not document_id or not drive_file_id:
                continue
            result = await ensure_drive_watch(
                db,
                user_id=user_id,
                document_id=document_id,
                drive_file_id=drive_file_id,
                force=True,
            )
            if result:
                renewed.append(document_id)
        await set_job_output(
            db,
            job_id,
            {
                "renewed_documents": renewed,
                "checked": checked_count,
            },
        )
        await update_job_status(db, job_id, "completed", progress=make_progress("completed"))
    except Exception as exc:
        await update_job_status(db, job_id, "failed", error=str(exc))
        raise


async def process_generate_blog_job(
    db: Database,
    job_id: str,
    user_id: str,
    document_id: str,
    options: Optional[Dict[str, Any]] = None,
):
    """Orchestrate outline -> chapters -> SEO -> compose pipeline."""
    options = options or {}
    recent_logs: list[str] = []
    MAX_LOGS = 40

    def _log(msg: str) -> None:
        safe = str(msg)
        recent_logs.append(safe[:280])
        if len(recent_logs) > MAX_LOGS:
            del recent_logs[0: len(recent_logs) - MAX_LOGS]

    def _progress(stage: str) -> Dict[str, Any]:
        return make_progress(stage, recent_logs=list(recent_logs))

    try:
        _log("Loading document payload")
        await update_job_status(db, job_id, "processing", progress=_progress("loading_document"))
        doc = await get_document(db, document_id, user_id=user_id)
        if not doc:
            raise ValueError("Document not found")
        text = (doc.get("raw_text") or "").strip()
        if not text:
            raise ValueError("Document missing raw text; ingest text or transcript first")
        metadata = _parse_document_metadata(doc)

        try:
            max_sections = max(1, min(12, int(options.get("max_sections", 5))))
        except Exception:
            max_sections = 5
        try:
            target_chapters = max(1, min(12, int(options.get("target_chapters", max_sections))))
        except Exception:
            target_chapters = max_sections
        tone = str(options.get("tone") or "informative")
        include_images = bool(options.get("include_images", True))
        section_index = options.get("section_index")

        _log("Generating outline")
        outline = generate_outline(text, max_sections)
        await record_usage_event(db, user_id, job_id, "outline", {"sections": len(outline)})
        await update_job_status(db, job_id, "processing", progress=_progress("outline"))

        _log("Organizing chapters")
        chapters = organize_chapters(text, target_chapters)
        if not chapters and outline:
            chapters = [{"title": item.get("title"), "summary": item.get("summary")} for item in outline if item]
        if not chapters:
            chapters = [{"title": default_title_from_outline([]), "summary": textwrap.shorten(text, width=360, placeholder="…")}]
        await record_usage_event(db, user_id, job_id, "chapters", {"chapters": len(chapters)})
        await update_job_status(db, job_id, "processing", progress=_progress("chapters"))

        _log("Generating SEO metadata")
        seo_meta = generate_seo_metadata(text, outline)

        _log("Composing markdown body")
        composed = compose_blog(chapters, tone=tone)
        markdown_body = composed.get("markdown", "")
        word_count = composed.get("meta", {}).get("word_count", len(markdown_body.split()))
        await record_usage_event(db, user_id, job_id, "compose", {"tone": tone, "word_count": word_count})

        html_body = markdown_to_html(markdown_body)
        image_prompts = generate_image_prompts(chapters) if include_images else []

        frontmatter = {
            "title": seo_meta.get("title") or default_title_from_outline(outline),
            "description": seo_meta.get("description"),
            "slug": seo_meta.get("slug") or f"{document_id[:8]}-draft",
            "tags": seo_meta.get("keywords", []),
            "hero_image": seo_meta.get("hero_image"),
        }

        sections = []
        for idx, chapter in enumerate(chapters):
            section = {
                "order": idx,
                "title": chapter.get("title"),
                "summary": chapter.get("summary"),
            }
            if include_images and idx < len(image_prompts):
                section["image_prompt"] = image_prompts[idx]
            sections.append(section)

        pipeline_output = {
            "document_id": document_id,
            "content_format": "mdx",
            "frontmatter": frontmatter,
            "body": {
                "mdx": markdown_body,
                "html": html_body,
            },
            "outline": outline,
            "chapters": chapters,
            "sections": sections,
            "seo": seo_meta,
            "assets": {
                "images": image_prompts,
                "media": [],
            },
            "options": {
                "tone": tone,
                "max_sections": max_sections,
                "target_chapters": target_chapters,
                "include_images": include_images,
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        _log("Persisting pipeline output")
        await set_job_output(db, job_id, pipeline_output)
        version_row = await create_document_version(
            db,
            document_id=document_id,
            user_id=user_id,
            content_format=pipeline_output["content_format"],
            frontmatter=frontmatter,
            body_mdx=markdown_body,
            body_html=html_body,
            outline=outline,
            chapters=chapters,
            sections=sections,
            assets=pipeline_output["assets"],
        )
        version_id = version_row.get("version_id")

        metadata["latest_generation"] = {
            "job_id": job_id,
            "title": frontmatter["title"],
            "slug": frontmatter["slug"],
            "generated_at": pipeline_output["generated_at"],
            "version_id": version_id,
            "section_index": section_index,
        }
        metadata["latest_outline"] = outline
        metadata["latest_chapters"] = chapters
        metadata["latest_sections"] = sections
        await update_document(
            db,
            document_id,
            {
                "metadata": metadata,
                "frontmatter": frontmatter,
                "content_format": pipeline_output["content_format"],
                "latest_version_id": version_id,
            },
        )
        await record_usage_event(db, user_id, job_id, "persist", {"sections": len(sections)})
        await _safe_pipeline_event(
            db,
            user_id,
            job_id,
            event_type="generate_blog",
            stage="generate_blog.persist",
            status="completed",
            message="Blog draft generated",
            data={"document_id": document_id, "version_id": version_id},
            notify_level="success",
            notify_text="Blog draft generated",
            notify_context={"document_id": document_id},
        )

        await update_job_status(db, job_id, "completed", progress=_progress("completed"))
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="success", text="Blog draft generated")
        except Exception:
            pass
    except Exception as exc:
        await update_job_status(db, job_id, "failed", error=str(exc))
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="Blog generation failed")
        except Exception:
            pass
        raise

async def handle_queue_message(message: Dict[str, Any], db: Database, queue_producer: Optional[QueueProducer] = None):
    """Handle a message from the queue."""
    job_id = message.get("job_id")
    user_id = message.get("user_id")
    
    if not job_id or not user_id:
        app_logger.error("Invalid queue message: missing job_id or user_id")
        return
    
    job_type = message.get("job_type")
    app_logger.info(f"Processing queue message for job {job_id} type={job_type}")

    job_row = await get_job(db, job_id, user_id)
    payload_data: Dict[str, Any] = {}
    if job_row:
        payload_raw = job_row.get("payload")
        if isinstance(payload_raw, str):
            try:
                payload_data = json.loads(payload_raw)
            except Exception:
                payload_data = {}
        elif isinstance(payload_raw, dict):
            payload_data = payload_raw

    try:
        if job_type == "ingest_youtube":
            document_id = message.get("document_id") or (job_row.get("document_id") if job_row else None)
            video_id = message.get("youtube_video_id") or payload_data.get("youtube_video_id")
            if not document_id or not video_id:
                app_logger.error("YouTube ingestion message missing document_id or youtube_video_id")
                try:
                    await update_job_status(db, job_id, "failed", error="Missing document_id or youtube_video_id")
                    try:
                        await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="Job failed: invalid YouTube ingestion payload")
                    except Exception:
                        pass
                except Exception:
                    pass
                return
            await process_ingest_youtube_job(db, job_id, user_id, document_id, video_id, payload_data)
        elif job_type == "ingest_drive":
            document_id = message.get("document_id") or (job_row.get("document_id") if job_row else None)
            drive_file_id = message.get("drive_file_id") or payload_data.get("drive_file_id")
            if not document_id or not drive_file_id:
                app_logger.error("Drive ingestion message missing document_id or drive_file_id")
                try:
                    await update_job_status(db, job_id, "failed", error="Missing Drive metadata")
                    try:
                        await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="Job failed: invalid Drive ingest payload")
                    except Exception:
                        pass
                except Exception:
                    pass
                return
            await process_ingest_drive_job(
                db,
                job_id,
                user_id,
                document_id,
                drive_file_id,
                message.get("drive_revision_id") or payload_data.get("drive_revision_id"),
            )
        elif job_type == "ingest_text":
            document_id = message.get("document_id") or (job_row.get("document_id") if job_row else None)
            if not document_id:
                app_logger.error("Text ingestion message missing document_id")
                try:
                    await update_job_status(db, job_id, "failed", error="Missing document_id")
                    try:
                        await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="Job failed: invalid text ingestion payload")
                    except Exception:
                        pass
                except Exception:
                    pass
                return
            await process_ingest_text_job(db, job_id, user_id, document_id)
        elif job_type == "optimize_drive":
            document_id = message.get("document_id") or (job_row.get("document_id") if job_row else None)
            if not document_id:
                app_logger.error("Optimize job missing document_id")
                try:
                    await update_job_status(db, job_id, "failed", error="Missing document_id")
                    try:
                        await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="Job failed: missing document reference")
                    except Exception:
                        pass
                except Exception:
                    pass
                return
            try:
                drive_folder = await _resolve_drive_folder(db, document_id, user_id)
            except Exception as exc:
                app_logger.error(f"Failed resolving Drive folder for job {job_id}: {exc}")
                try:
                    await update_job_status(db, job_id, "failed", error=str(exc))
                    try:
                        await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="Job failed: invalid Drive document")
                    except Exception:
                        pass
                except Exception:
                    pass
                return
            await process_optimization_job(
                db=db,
                job_id=job_id,
                user_id=user_id,
                drive_folder=drive_folder,
                extensions=message.get("extensions") or payload_data.get("extensions") or [],
                overwrite=message.get("overwrite", payload_data.get("overwrite", False)),
                skip_existing=message.get("skip_existing", payload_data.get("skip_existing", True)),
                cleanup_originals=message.get("cleanup_originals", payload_data.get("cleanup_originals", False)),
                max_retries=message.get("max_retries", payload_data.get("max_retries", 3)),
            )
        elif job_type == "generate_blog":
            document_id = message.get("document_id") or (job_row.get("document_id") if job_row else None)
            if not document_id:
                app_logger.error("Generate blog job missing document_id")
                try:
                    await update_job_status(db, job_id, "failed", error="Missing document_id")
                    try:
                        await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="Job failed: missing document reference")
                    except Exception:
                        pass
                except Exception:
                    pass
                return
            await process_generate_blog_job(
                db,
                job_id,
                user_id,
                document_id,
                message.get("options") or payload_data.get("options") or {},
            )
        elif job_type == "drive_change_poll":
            doc_ids = message.get("document_ids") or payload_data.get("document_ids")
            await process_drive_change_poll_job(db, job_id, user_id, doc_ids, queue_producer)
        elif job_type == "drive_watch_renewal":
            await process_drive_watch_renewal_job(db, job_id, user_id)
        else:
            error_msg = f"Unknown job_type '{job_type}' for job {job_id}"
            app_logger.error(error_msg)
            await _handle_job_failure(db, job_row, error_msg, message, queue_producer)
    except Exception as e:
        app_logger.error(f"Failed to process job {job_id}: {e}", exc_info=True)
        await _handle_job_failure(db, job_row, str(e), message, queue_producer)


async def run_inline_queue_consumer(poll_interval: float = 1.0, recover_pending: bool = True):
    """Inline consumer that polls the DB for pending jobs."""
    from src.workers.api.config import settings
    from src.workers.api.database import get_pending_jobs

    app_logger.info("Starting inline queue consumer (DB polling)")
    db = Database(db=settings.d1_database)

    async def _build_message(job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        elif not isinstance(payload, dict):
            payload = {}
        message = {
            "job_id": job.get("job_id"),
            "user_id": job.get("user_id"),
            "job_type": job.get("job_type"),
            "document_id": job.get("document_id"),
        }
        # Only merge payload fields that don't conflict with message structure
        for key, value in (payload or {}).items():
            if key not in ("job_id", "user_id", "job_type", "document_id"):
                message[key] = value
        return message

    async def _process_jobs(jobs: List[Dict[str, Any]]):
        for job in jobs:
            try:
                msg = await _build_message(job)
                await handle_queue_message(msg, db)
            except Exception:
                app_logger.exception("Inline consumer error", extra={"job_id": job.get("job_id")})

    if recover_pending:
        pending = await get_pending_jobs(db, limit=50)
        app_logger.info(f"Recovered {len(pending)} jobs on startup")
        await _process_jobs(pending)

    try:
        while True:
            jobs = await get_pending_jobs(db, limit=10)
            if not jobs:
                await asyncio.sleep(poll_interval)
                continue
            await _process_jobs(jobs)
    except KeyboardInterrupt:
        app_logger.info("Inline consumer interrupted; exiting")


def main():
    """CLI entry point for worker consumer."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Cloudflare Worker Queue Consumer")
    parser.add_argument(
        "--inline",
        action="store_true",
        help="Run in inline queue mode (for local development with USE_INLINE_QUEUE=true)"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Poll interval in seconds for inline queue mode (default: 1.0)"
    )
    parser.add_argument(
        "--no-recover",
        action="store_true",
        help="Skip recovering pending jobs from database on startup"
    )
    
    args = parser.parse_args()
    
    if args.inline:
        # Run inline queue consumer
        asyncio.run(run_inline_queue_consumer(
            poll_interval=args.poll_interval,
            recover_pending=not args.no_recover
        ))
    else:
        parser.print_help()
        print("\nNote: For Cloudflare Workers deployment, the consumer runs automatically via queue bindings.")
        print("For local development, use: python workers/consumer.py --inline")


if __name__ == "__main__":
    main()
