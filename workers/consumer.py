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
from typing import Dict, Any, Optional
import asyncio
import functools

from core.drive_utils import (
    download_images,
    upload_images,
    delete_images,
    get_folder_name,
    extract_folder_id_from_input,
    is_valid_drive_file_id,
)
from core.image_processor import process_image
from api.database import Database, update_job_status
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
        # Update status to processing
        await update_job_status(db, job_id, "processing", progress=make_progress("extracting_folder_id"))
        
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
        await update_job_status(db, job_id, "processing", progress=make_progress("downloading"))
        
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
        
        await update_job_status(db, job_id, "processing", progress=make_progress(
            "downloading",
            downloaded=len(downloaded),
            download_failed=len(failed_downloads),
        ))
        
        # Optimize images
        await update_job_status(db, job_id, "processing", progress=make_progress(
            "optimizing",
            downloaded=len(downloaded),
            download_failed=len(failed_downloads),
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
                await update_job_status(db, job_id, "processing", progress=make_progress(
                    "optimizing",
                    downloaded=len(downloaded),
                    optimized=len(optimized),
                    skipped=len(skipped),
                    download_failed=len(failed_downloads),
                    processing_failed=len(failed_processing),
                ))
        
        # Upload optimized images
        await update_job_status(db, job_id, "processing", progress=make_progress(
            "uploading",
            downloaded=len(downloaded),
            optimized=len(optimized),
            skipped=len(skipped),
            download_failed=len(failed_downloads),
            processing_failed=len(failed_processing),
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
        
        await update_job_status(db, job_id, "processing", progress=make_progress(
            "uploading",
            downloaded=len(downloaded),
            optimized=len(optimized),
            skipped=len(skipped),
            uploaded=len(uploaded),
            download_failed=len(failed_downloads),
            upload_failed=len(failed_uploads),
            processing_failed=len(failed_processing),
        ))
        
        # Delete originals if requested
        deleted_count = 0
        if cleanup_originals:
            await update_job_status(db, job_id, "processing", progress=make_progress(
                "cleaning_up",
                downloaded=len(downloaded),
                optimized=len(optimized),
                skipped=len(skipped),
                uploaded=len(uploaded),
                download_failed=len(failed_downloads),
                upload_failed=len(failed_uploads),
                processing_failed=len(failed_processing),
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
        ))

        app_logger.info(f"Job {job_id} completed successfully")
        # Create success notification
        try:
            await notify_job(db, user_id=user_id, job_id=job_id, level="success", text=f"Job {job_id} completed")
        except Exception:
            pass
        
    except Exception as e:
        app_logger.error(f"Job {job_id} failed: {e}", exc_info=True)
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


async def handle_queue_message(message: Dict[str, Any], db: Database):
    """Handle a message from the queue."""
    job_id = message.get("job_id")
    user_id = message.get("user_id")
    
    if not job_id or not user_id:
        app_logger.error("Invalid queue message: missing job_id or user_id")
        return
    
    drive_folder = message.get("drive_folder")
    if not drive_folder or not drive_folder.strip():
        app_logger.error(
            f"Invalid queue message: missing or empty drive_folder for job_id={job_id}, user_id={user_id}",
            extra={"job_id": job_id, "user_id": user_id, "drive_folder": drive_folder}
        )
        return
    
    app_logger.info(f"Processing queue message for job {job_id}")
    
    try:
        await process_optimization_job(
            db=db,
            job_id=job_id,
            user_id=user_id,
            drive_folder=drive_folder,
            extensions=message.get("extensions", []),
            overwrite=message.get("overwrite", False),
            skip_existing=message.get("skip_existing", True),
            cleanup_originals=message.get("cleanup_originals", False),
            max_retries=message.get("max_retries", 3)
        )
    except Exception as e:
        app_logger.error(f"Failed to process job {job_id}: {e}", exc_info=True)
        # The job status is already updated to failed in process_optimization_job
        raise

