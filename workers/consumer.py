"""
Cloudflare Worker consumer for processing image optimization jobs from the queue.
"""

import os
import json
import re
import logging
from typing import Dict, Any
from datetime import datetime

from core.drive_utils import (
    get_drive_service,
    download_images,
    upload_images,
    delete_images,
    get_folder_name
)
from core.image_processor import process_image
from api.database import Database, update_job_status, get_job
from api.config import settings
from api.app_logging import setup_logging, get_logger

# Set up logging
logger = setup_logging(level="INFO", use_json=True)
app_logger = get_logger(__name__)


def extract_folder_id_from_input(folder_input: str) -> str:
    """Extract folder ID from share link or return as-is if already an ID."""
    match = re.search(r"/folders/([\w-]+)", folder_input)
    if match:
        return match.group(1)
    if re.match(r"^[\w-]{10,}$", folder_input):
        return folder_input
    raise ValueError("Invalid Google Drive folder link or ID.")


def is_valid_drive_file_id(file_id: str) -> bool:
    """Check if a string looks like a valid Google Drive file ID."""
    if not file_id or len(file_id) < 20:
        return False
    if not re.match(r"^[a-zA-Z0-9_-]+$", file_id):
        return False
    return True


def sanitize_folder_name(folder_name: str) -> str:
    """Sanitize folder name for use in filenames."""
    return re.sub(r'[^a-zA-Z0-9_-]+', '-', folder_name.strip().lower())


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
    """Process an image optimization job."""
    try:
        # Update status to processing
        await update_job_status(db, job_id, "processing", progress={
            "stage": "extracting_folder_id",
            "downloaded": 0,
            "optimized": 0,
            "skipped": 0,
            "uploaded": 0,
            "deleted": 0,
            "download_failed": 0,
            "upload_failed": 0
        })
        
        # Extract folder ID
        folder_id = extract_folder_id_from_input(drive_folder)
        
        # Get folder name for SEO prefix
        folder_name = get_folder_name(folder_id) or "optimized"
        folder_name_clean = sanitize_folder_name(folder_name)
        
        # Set up directories
        output_dir = f"optimized_{folder_name_clean}"
        temp_dir = 'temp_download'
        os.makedirs(temp_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        
        # Convert extensions to set with dots
        extensions_set = set(
            e.lower() if e.startswith('.') else f'.{e.lower()}'
            for e in extensions
        )
        
        # Download images
        await update_job_status(db, job_id, "processing", progress={
            "stage": "downloading",
            "downloaded": 0,
            "optimized": 0,
            "skipped": 0,
            "uploaded": 0,
            "deleted": 0,
            "download_failed": 0,
            "upload_failed": 0
        })
        
        downloaded, failed_downloads = download_images(
            folder_id,
            temp_dir,
            extensions=extensions_set,
            fail_log_path='failures.log',
            max_retries=max_retries
        )
        
        await update_job_status(db, job_id, "processing", progress={
            "stage": "downloading",
            "downloaded": len(downloaded),
            "optimized": 0,
            "skipped": 0,
            "uploaded": 0,
            "deleted": 0,
            "download_failed": len(failed_downloads),
            "upload_failed": 0
        })
        
        # Optimize images
        await update_job_status(db, job_id, "processing", progress={
            "stage": "optimizing",
            "downloaded": len(downloaded),
            "optimized": 0,
            "skipped": 0,
            "uploaded": 0,
            "deleted": 0,
            "download_failed": len(failed_downloads),
            "upload_failed": 0
        })
        
        optimized = []
        skipped = []
        
        for fname in downloaded:
            input_path = os.path.join(temp_dir, fname)
            out_path, status = process_image(
                input_path,
                output_dir,
                overwrite=overwrite,
                skip_existing=skip_existing,
                versioned=False,
                seo_prefix=folder_name_clean
            )
            if status == 'skipped':
                skipped.append(fname)
            else:
                optimized.append(fname)
            
            # Update progress
            await update_job_status(db, job_id, "processing", progress={
                "stage": "optimizing",
                "downloaded": len(downloaded),
                "optimized": len(optimized),
                "skipped": len(skipped),
                "uploaded": 0,
                "deleted": 0,
                "download_failed": len(failed_downloads),
                "upload_failed": 0
            })
        
        # Upload optimized images
        await update_job_status(db, job_id, "processing", progress={
            "stage": "uploading",
            "downloaded": len(downloaded),
            "optimized": len(optimized),
            "skipped": len(skipped),
            "uploaded": 0,
            "deleted": 0,
            "download_failed": len(failed_downloads),
            "upload_failed": 0
        })
        
        uploaded, failed_uploads = upload_images(
            output_dir,
            folder_id,
            extensions=['.webp'],
            fail_log_path='failures.log',
            max_retries=max_retries
        )
        
        await update_job_status(db, job_id, "processing", progress={
            "stage": "uploading",
            "downloaded": len(downloaded),
            "optimized": len(optimized),
            "skipped": len(skipped),
            "uploaded": len(uploaded),
            "deleted": 0,
            "download_failed": len(failed_downloads),
            "upload_failed": len(failed_uploads)
        })
        
        # Delete originals if requested
        deleted_count = 0
        if cleanup_originals:
            await update_job_status(db, job_id, "processing", progress={
                "stage": "cleaning_up",
                "downloaded": len(downloaded),
                "optimized": len(optimized),
                "skipped": len(skipped),
                "uploaded": len(uploaded),
                "deleted": 0,
                "download_failed": len(failed_downloads),
                "upload_failed": len(failed_uploads)
            })
            
            original_file_ids = []
            for fname in downloaded:
                name_part = os.path.splitext(fname)[0]
                parts = name_part.rsplit('_', 1)
                if len(parts) == 2:
                    file_id = parts[1]
                    if is_valid_drive_file_id(file_id):
                        original_file_ids.append(file_id)
            
            if original_file_ids:
                delete_images(folder_id, original_file_ids)
                deleted_count = len(original_file_ids)
        
        # Clean up local directories
        import shutil
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        
        # Mark as completed
        await update_job_status(db, job_id, "completed", progress={
            "stage": "completed",
            "downloaded": len(downloaded),
            "optimized": len(optimized),
            "skipped": len(skipped),
            "uploaded": len(uploaded),
            "deleted": deleted_count,
            "download_failed": len(failed_downloads),
            "upload_failed": len(failed_uploads)
        })
        
        app_logger.info(f"Job {job_id} completed successfully")
        
    except Exception as e:
        app_logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        await update_job_status(
            db,
            job_id,
            "failed",
            error=str(e)
        )
        raise


async def handle_queue_message(message: Dict[str, Any], db: Database):
    """Handle a message from the queue."""
    job_id = message.get("job_id")
    user_id = message.get("user_id")
    
    if not job_id or not user_id:
        app_logger.error("Invalid queue message: missing job_id or user_id")
        return
    
    app_logger.info(f"Processing queue message for job {job_id}")
    
    try:
        await process_optimization_job(
            db=db,
            job_id=job_id,
            user_id=user_id,
            drive_folder=message.get("drive_folder"),
            extensions=message.get("extensions", []),
            overwrite=message.get("overwrite", False),
            skip_existing=message.get("skip_existing", True),
            cleanup_originals=message.get("cleanup_originals", True),
            max_retries=message.get("max_retries", 3)
        )
    except Exception as e:
        app_logger.error(f"Failed to process job {job_id}: {e}", exc_info=True)
        # The job status is already updated to failed in process_optimization_job
        raise

