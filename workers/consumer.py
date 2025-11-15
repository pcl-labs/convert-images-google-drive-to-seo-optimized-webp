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
from datetime import datetime, timezone

from core.drive_utils import (
    download_images,
    upload_images,
    delete_images,
    get_folder_name,
    extract_folder_id_from_input,
    is_valid_drive_file_id,
)
from core.image_processor import process_image
from api.database import (
    Database,
    update_job_status,
    update_document,
    set_job_output,
    record_usage_event,
    get_document,
    create_document_version,
    get_job,
)
from api.config import settings
from api.google_oauth import build_youtube_service_for_user
from core.youtube_captions import fetch_captions_text, YouTubeCaptionsError
from core.ai_modules import (
    generate_outline,
    organize_chapters,
    compose_blog,
    default_title_from_outline,
    generate_seo_metadata,
    generate_image_prompts,
    markdown_to_html,
)
from api.notifications import notify_job
from api.app_logging import setup_logging, get_logger
from core.filename_utils import FILENAME_ID_SEPARATOR, sanitize_folder_name, parse_download_name, make_output_dir_name
from core.constants import TEMP_DIR, FAIL_LOG_PATH
from core.extension_utils import normalize_extensions, detect_extensions_in_dir
from api.google_oauth import build_drive_service_for_user

# Set up logging
logger = setup_logging(level="INFO", use_json=True)
app_logger = get_logger(__name__)




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


async def _resolve_drive_folder(db: Database, document_id: str, user_id: str) -> str:
    doc = await get_document(db, document_id, user_id=user_id)
    if not doc:
        raise ValueError("Document not found for optimize job")
    metadata = _parse_document_metadata(doc)
    source_type = doc.get("source_type")
    if source_type not in {"drive", "drive_folder"}:
        raise ValueError("Document is not associated with a Drive folder")
    folder_id = doc.get("source_ref") or metadata.get("drive_folder_id")
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
                await asyncio.to_thread(delete_images, folder_id, original_file_ids, service)
                deleted_count = len(original_file_ids)
                log_step(f"Cleanup finished: deleted {deleted_count} originals")
            else:
                app_logger.warning(
                    f"No valid file IDs extracted for cleanup from {len(downloaded)} downloaded files",
                    extra={"downloaded_count": len(downloaded)}
                )
        
        # Clean up local directories
        import shutil
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        
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
        # Prefer authoritative API duration if provided in job payload/metadata; transcript duration is a fallback.
        payload_duration = _normalize_duration(job_payload.get("duration_s") or payload_metadata.get("duration_seconds"))

        document = await get_document(db, document_id, user_id=user_id)
        if not document:
            raise ValueError("Document not found")
        doc_metadata = _parse_document_metadata(document)
        frontmatter = document.get("frontmatter")
        if isinstance(frontmatter, str):
            try:
                frontmatter = json.loads(frontmatter)
            except Exception:
                frontmatter = {}
        if not isinstance(frontmatter, dict):
            frontmatter = {}

        # Prepare config and fetch captions via official YouTube API
        langs_raw = settings.transcript_langs
        if isinstance(langs_raw, str):
            langs = [s.strip() for s in langs_raw.split(",") if s.strip()]
        else:
            langs = langs_raw or ["en"]
        try:
            yt_service = await build_youtube_service_for_user(db, user_id)  # type: ignore
        except ValueError as exc:
            await update_job_status(db, job_id, "failed", error=str(exc))
            return
        try:
            cap = await asyncio.to_thread(fetch_captions_text, yt_service, youtube_video_id, langs)
        except YouTubeCaptionsError as exc:
            await update_job_status(db, job_id, "failed", error=str(exc))
            try:
                await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="YouTube ingestion failed: captions unavailable")
            except Exception:
                pass
            return
        if not cap.get("success"):
            await update_job_status(db, job_id, "failed", error=str(cap.get("error") or "captions_unavailable"))
            try:
                await notify_job(db, user_id=user_id, job_id=job_id, level="error", text="YouTube ingestion failed: captions unavailable")
            except Exception:
                pass
            return

        text = (cap.get("text") or "").strip()
        source = cap.get("source") or "captions"
        lang = cap.get("lang") or "en"
        # Duration: authoritative API value provided in job payload/metadata
        duration_s = payload_duration

        # Validate required fields
        if duration_s is None:
            error_msg = "Transcript fetch succeeded but duration_s is missing"
            await update_job_status(db, job_id, "failed", error=error_msg)
            try:
                await notify_job(db, user_id=user_id, job_id=job_id, level="error", text=f"YouTube ingestion failed: {error_msg}")
            except Exception:
                pass
            return

        # Record usage events (transcribe only; no external downloads here)
        try:
            await record_usage_event(
                db,
                user_id,
                job_id,
                "transcribe",
                {
                    "engine": "captions_api",
                    "duration_s": duration_s,
                },
            )
        except Exception:
            pass

        # Merge metadata/frontmatter
        now_iso = datetime.now(timezone.utc).isoformat()
        video_meta = {}
        if isinstance(doc_metadata.get("youtube"), dict):
            video_meta.update(doc_metadata["youtube"])
        if isinstance(payload_metadata, dict):
            video_meta.update(payload_metadata)
        video_meta["duration_seconds"] = duration_s
        video_meta.setdefault("video_id", youtube_video_id)
        video_meta.setdefault("fetched_at", now_iso)

        doc_metadata["source"] = "youtube"
        doc_metadata["video_id"] = youtube_video_id
        doc_metadata["lang"] = lang
        doc_metadata["chars"] = len(text)
        doc_metadata["updated_at"] = now_iso
        doc_metadata["transcript_source"] = source
        doc_metadata["youtube"] = video_meta
        doc_metadata.setdefault("url", payload_metadata.get("url"))
        doc_metadata.setdefault("title", payload_metadata.get("title") or frontmatter.get("title"))

        transcript_meta = {
            "source": source,
            "lang": lang,
            "chars": len(text),
            "duration_s": duration_s,
            "fetched_at": now_iso,
        }
        doc_metadata["transcript"] = transcript_meta
        doc_metadata["latest_ingest_job_id"] = job_id

        frontmatter = {**frontmatter, **(payload_frontmatter if isinstance(payload_frontmatter, dict) else {})}
        if "title" not in frontmatter and payload_metadata.get("title"):
            frontmatter["title"] = payload_metadata.get("title")

        # Update document with transcript: write raw_text first to ensure it's persisted
        await update_document(db, document_id, {"raw_text": text})
        await update_document(
            db,
            document_id,
            {
                "raw_text": text,
                "metadata": doc_metadata,
                "frontmatter": frontmatter,
                "content_format": "youtube",
            },
        )
        # Safeguard: ensure raw_text is set (some backends may drop empty updates)
        try:
            refreshed = await get_document(db, document_id, user_id=user_id)
            if not refreshed or not (refreshed.get("raw_text") or "").strip():
                await update_document(db, document_id, {"raw_text": text})
        except Exception:
            pass
        # Final assurance: direct SQL update for raw_text to handle any edge cases in fallback DBs
        try:
            await db.execute("UPDATE documents SET raw_text = ?, updated_at = datetime('now') WHERE document_id = ?", (text, document_id))
        except Exception:
            pass

        # Set job output summary
        out = {
            "document_id": document_id,
            "youtube_video_id": youtube_video_id,
            "transcript": transcript_meta,
            "metadata": {
                "frontmatter": frontmatter,
                "youtube": video_meta,
            },
        }
        await set_job_output(db, job_id, out)

        await update_job_status(db, job_id, "completed", progress=make_progress("completed"))
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="success", text=f"Ingested YouTube {youtube_video_id}")
        except Exception:
            pass
    except Exception as e:
        await update_job_status(db, job_id, "failed", error=str(e))
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="error", text=f"YouTube ingestion failed")
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

async def handle_queue_message(message: Dict[str, Any], db: Database):
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
        else:
            app_logger.error(f"Unknown job_type '{job_type}' for job {job_id}")
    except Exception as e:
        app_logger.error(f"Failed to process job {job_id}: {e}", exc_info=True)
        raise


async def run_inline_queue_consumer(poll_interval: float = 1.0, recover_pending: bool = True):
    """Inline consumer that polls the DB for pending jobs."""
    from api.config import settings
    from api.database import get_pending_jobs

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
