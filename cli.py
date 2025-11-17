"""
Main CLI entry point for Quill.
"""

import argparse
import sys
import os
import json
import traceback
from core.drive_utils import extract_folder_id_from_input, is_valid_drive_file_id
from core.filename_utils import sanitize_folder_name, parse_download_name, FILENAME_ID_SEPARATOR, make_output_dir_name
from core.constants import TEMP_DIR, FAIL_LOG_PATH, DEFAULT_EXTENSIONS
from core.extension_utils import detect_extensions_in_dir

def load_cache(cache_path):
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, IOError) as e:
            print(f"Warning: Failed to load cache file '{cache_path}': {e}")
            # Rename corrupted file to allow fresh cache creation
            try:
                backup_path = f"{cache_path}.bad"
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                os.rename(cache_path, backup_path)
                print(f"Renamed corrupted cache file to '{backup_path}'")
            except Exception as rename_error:
                # If rename fails, try to remove the file
                try:
                    os.remove(cache_path)
                    print(f"Removed corrupted cache file '{cache_path}'")
                except Exception as remove_error:
                    print(f"Warning: Could not remove corrupted cache file: {remove_error}")
    return {"downloaded": [], "processed": [], "failed": []}

def save_cache(cache, cache_path):
    """
    Save cache to file using atomic write pattern.
    
    Args:
        cache: Dictionary to save as JSON
        cache_path: Path to cache file
        
    Returns:
        True if successful
        
    Raises:
        OSError: If file operations fail (permissions, disk full, etc.)
        IOError: If I/O operations fail
        Exception: For other unexpected errors
    """
    import tempfile
    
    # Ensure parent directory exists
    cache_dir = os.path.dirname(cache_path)
    if cache_dir and not os.path.exists(cache_dir):
        try:
            os.makedirs(cache_dir, mode=0o755, exist_ok=True)
        except (OSError, IOError) as e:
            error_msg = f"Failed to create cache directory '{cache_dir}': {type(e).__name__}: {e}"
            print(f"Error: {error_msg}")
            raise OSError(error_msg) from e
    
    # Create temporary file in the same directory as target
    # This ensures atomic replacement works even across filesystems
    cache_dir = cache_dir or os.path.dirname(os.path.abspath(cache_path)) or '.'
    temp_fd = None
    temp_path = None
    
    try:
        # Create temporary file with appropriate permissions
        temp_fd, temp_path = tempfile.mkstemp(
            dir=cache_dir,
            prefix=os.path.basename(cache_path) + '.tmp.',
            suffix=''
        )
        
        # Write cache data to temporary file
        try:
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                json.dump(cache, f, indent=2)
                # Ensure data is flushed to disk
                f.flush()
                os.fsync(f.fileno())
        except (OSError, IOError) as e:
            # Close the file descriptor if json.dump failed
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            error_msg = f"Failed to write cache data to temporary file '{temp_path}': {type(e).__name__}: {e}"
            print(f"Error: {error_msg}")
            raise IOError(error_msg) from e
        
        # Atomically replace target file with temporary file
        try:
            os.replace(temp_path, cache_path)
            temp_path = None  # Mark as successfully moved
        except (OSError, IOError) as e:
            error_msg = f"Failed to atomically replace cache file '{cache_path}': {type(e).__name__}: {e}"
            print(f"Error: {error_msg}")
            raise OSError(error_msg) from e
            
    except (OSError, IOError) as e:
        # Re-raise OSError/IOError as-is (already handled above)
        raise
    except Exception as e:
        # Catch any other unexpected errors
        error_msg = f"Unexpected error while saving cache to '{cache_path}': {type(e).__name__}: {e}"
        print(f"Error: {error_msg}")
        raise Exception(error_msg) from e
    finally:
        # Clean up temporary file if it still exists (write or replace failed)
        if temp_path is not None and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError as cleanup_error:
                print(f"Warning: Failed to clean up temporary cache file '{temp_path}': {cleanup_error}")
    
    return True

def safe_remove_directory(dir_path):
    """
    Safely remove a directory with error handling for read-only files.
    
    Args:
        dir_path: Path to the directory to remove
        
    Returns:
        True if removal succeeded, False otherwise
    """
    import shutil
    import stat
    
    def handle_remove_readonly(func, path, exc_info):
        """
        Error handler for shutil.rmtree that changes permissions and retries.
        
        Args:
            func: The function that failed (os.remove, os.rmdir, etc.)
            path: The path that caused the failure
            exc_info: Tuple of (exception_type, exception_value, traceback)
        """
        exc_type, exc_value, exc_traceback = exc_info
        
        # Check if the error is due to read-only permissions (PermissionError or OSError with errno 13)
        is_permission_error = (
            isinstance(exc_value, PermissionError) or
            (isinstance(exc_value, OSError) and exc_value.errno == 13) or
            not os.access(path, os.W_OK)
        )
        
        if is_permission_error:
            # Change permissions to allow removal
            try:
                # Make file/directory writable
                os.chmod(path, stat.S_IWRITE | stat.S_IWUSR | stat.S_IREAD | stat.S_IRUSR)
                # Retry the operation
                func(path)
            except Exception as chmod_error:
                # If chmod fails, log and continue (don't re-raise to avoid crashing)
                print(f"  Warning: Could not change permissions for '{path}': {type(chmod_error).__name__}: {chmod_error}")
        else:
            # If it's not a permission issue, re-raise the original exception
            raise exc_value
    
    if not os.path.exists(dir_path):
        return True
    
    try:
        shutil.rmtree(dir_path, onerror=handle_remove_readonly)
        return True
    except Exception as e:
        print(f"Error: Failed to remove directory '{dir_path}': {type(e).__name__}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Quill CLI")
    parser.add_argument('--drive-folder', required=False, help='Google Drive folder ID or share link')
    parser.add_argument('--upload-dir', help='Local directory of images to upload')
    parser.add_argument('--output-dir', default='optimized', help='Directory to save optimized images')
    parser.add_argument('--optimize', action='store_true', help='Optimize images (resize, compress, convert)')
    parser.add_argument('--resume', action='store_true', help='Resume from last cache')
    parser.add_argument('--upload-optimized', action='store_true', help='Upload optimized images to Drive')
    parser.add_argument('--cleanup', action='store_true', help='Prompt to delete originals after optimization')
    parser.add_argument('--ext', default='jpg,jpeg,png,bmp,tiff,heic', help='Comma-separated list of extensions to process')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite existing optimized files')
    parser.add_argument('--skip-existing', action='store_true', help='Skip files that are already optimized')
    parser.add_argument('--fail-log', default='failures.log', help='Path to log failed conversions')
    parser.add_argument('--cache-file', default='cache.json', help='Custom path for cache file')
    parser.add_argument('--config', help='Path to config.json for persistent settings')
    parser.add_argument('--reauth', action='store_true', help='Force new Google account auth')
    parser.add_argument('--dry-run', action='store_true', help='Preview actions without making changes')
    parser.add_argument('--test-mode', action='store_true', help='Mock Drive calls and file operations')
    parser.add_argument('--keep-temp', action='store_true', help='Keep temp download directory after processing')
    parser.add_argument('--max-retries', type=int, default=3, help='Number of times to retry failed operations')
    parser.add_argument('--versioned', action='store_true', help='Save versioned filenames if conflicts')

    args = parser.parse_args()

    # Handle reauth flag (do this before any folder ID logic)
    if args.reauth:
        print("Re-auth is now handled via the web UI. Please link your Google Drive account from the dashboard.")
        sys.exit(0)

    # Enforce --drive-folder is required for all other operations
    drive_folder = args.drive_folder
    if not drive_folder:
        drive_folder = input('Please enter a Google Drive folder share link or ID where your images are located: ').strip()
        if not drive_folder:
            print('Error: A Google Drive folder link or ID is required.')
            sys.exit(1)

    # Extract folder ID
    try:
        folder_id = extract_folder_id_from_input(drive_folder)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Fetch folder name for SEO-friendly filenames
    from core.drive_utils import get_folder_name
    try:
        folder_name = get_folder_name(folder_id)
        if not folder_name:
            folder_name = "optimized"
    except Exception as e:
        print(f"Warning: Failed to fetch folder name: {e}. Using default 'optimized'.")
        folder_name = "optimized"
    
    # Sanitize folder name for filenames
    folder_name_clean = sanitize_folder_name(folder_name)

    # Use a unique output directory per Drive folder
    output_dir = make_output_dir_name(folder_name)
    temp_dir = TEMP_DIR
    extensions = [e.strip().lower() for e in args.ext.split(',')] if args.ext else DEFAULT_EXTENSIONS
    print(f"Downloading images from Drive folder {folder_id} to {temp_dir}...")
    from core.drive_utils import download_images
    try:
        downloaded, failed = download_images(
            folder_id,
            temp_dir,
            extensions=extensions,
            fail_log_path=FAIL_LOG_PATH,
            max_retries=args.max_retries
        )
        print(f"Downloaded: {downloaded}")
        if failed:
            print(f"Failed to download: {failed}")
    except Exception as e:
        error_msg = f"Error: Failed to download images from Drive folder {folder_id}"
        print(f"\n{error_msg}")
        print(f"Exception details: {type(e).__name__}: {e}")
        print(f"\nTraceback:")
        traceback.print_exc()
        print(f"\nExiting due to download failure.")
        sys.exit(1)

    # Optimize images
    from core.image_processor import process_image
    os.makedirs(output_dir, exist_ok=True)
    optimized = []
    skipped = []
    failed = []
    for fname in downloaded:
        input_path = os.path.join(temp_dir, fname)
        try:
            out_path, status = process_image(
                input_path,
                output_dir,
                overwrite=args.overwrite,
                skip_existing=args.skip_existing,
                versioned=args.versioned,
                seo_prefix=folder_name_clean
            )
            # Only handle out_path/status on success
            if status == 'skipped':
                skipped.append(fname)
            else:
                optimized.append(fname)
        except Exception as e:
            error_msg = f"Failed to process image '{fname}': {type(e).__name__}: {e}"
            print(f"Error: {error_msg}")
            failed.append(fname)
            # Continue processing remaining files
            continue
    print(f"\nOptimization complete. {len(optimized)} images optimized, {len(skipped)} skipped (already optimized), {len(failed)} failed. Optimized images are in '{output_dir}'.\n")
    if failed:
        print(f"Failed to process: {failed}\n")

    # Automatically upload optimized images to the same Drive folder
    print(f"Uploading optimized images from {output_dir} to Drive folder {folder_id}...")
    from core.drive_utils import upload_images, delete_images
    uploaded = []
    failed_uploads = []
    try:
        # Detect actual extensions in output directory, with fallback defaults
        upload_extensions = detect_extensions_in_dir(output_dir)
        print(f"Detected extensions for upload: {upload_extensions}")
        uploaded, failed_uploads = upload_images(output_dir, folder_id, extensions=upload_extensions, fail_log_path=FAIL_LOG_PATH, max_retries=args.max_retries)
        print(f"\nUpload complete. {len(uploaded)} uploaded, {len(failed_uploads)} failed, {len(os.listdir(output_dir)) - len(uploaded)} skipped (already in Drive).\n")
    except Exception as e:
        error_msg = f"Failed to upload images: {type(e).__name__}: {e}"
        print(f"Error: {error_msg}")
        print(f"\nTraceback:")
        traceback.print_exc()
        # Set defaults: uploaded and failed_uploads are already empty lists
        # Continue execution but note the failure
        print(f"\nWarning: Upload failed. Continuing with cleanup. No images were uploaded.\n")

    # Automatically delete original images from Google Drive
    # Extract original file IDs from downloaded filenames
    original_file_ids = []
    print(f"\nExtracting file IDs from downloaded filenames...")
    for fname in downloaded:
        parsed = parse_download_name(fname, sep=FILENAME_ID_SEPARATOR)
        if parsed:
            _, file_id, _ = parsed
            if is_valid_drive_file_id(file_id):
                original_file_ids.append(file_id)
                print(f"  Extracted file ID: {file_id} from {fname}")
            else:
                print(f"  Warning: Invalid file ID '{file_id}' extracted from {fname}")
        else:
            print(f"  Warning: Could not extract file ID from {fname}")
    
    print(f"Found {len(original_file_ids)} valid file IDs for deletion")
    
    if original_file_ids:
        print("Automatically deleting original images from Google Drive...")
        try:
            deleted_ids, failed_ids = delete_images(folder_id, original_file_ids)
            deleted_count = len(deleted_ids)
            failed_count = len(failed_ids)
            if failed_count > 0:
                print(f"Deletion completed: {deleted_count} deleted, {failed_count} failed")
                if failed_ids:
                    print(f"Failed IDs: {', '.join(failed_ids[:10])}{'...' if len(failed_ids) > 10 else ''}")
            else:
                print(f"Original images deleted: {deleted_count} files")
        except Exception as e:
            print(f"\nError: Failed to delete original images from Google Drive.")
            print(f"Exception details: {type(e).__name__}: {e}")
            print(f"\nTraceback:")
            traceback.print_exc()
            print(f"\nExiting due to deletion failure.")
            sys.exit(1)
    else:
        print("No original file IDs found for cleanup.")
    
    # Calculate summary before cleanup
    skipped_upload_count = len(os.listdir(output_dir)) - len(uploaded) if os.path.exists(output_dir) else 0
    
    print("\nSummary:")
    print(f"  Downloaded: {len(downloaded)}")
    print(f"  Optimized: {len(optimized)}")
    print(f"  Skipped (already optimized): {len(skipped)}")
    print(f"  Failed to process: {len(failed)}")
    print(f"  Uploaded: {len(uploaded)}")
    print(f"  Skipped upload (already in Drive): {skipped_upload_count}")
    print(f"  Failed uploads: {len(failed_uploads)}")
    
    # Clean up local directories
    print("\nCleaning up local directories...")
    
    # Remove temp download directory
    if safe_remove_directory(temp_dir):
        print(f"Removed temp directory: {temp_dir}")
    
    # Remove optimized directory
    if safe_remove_directory(output_dir):
        print(f"Removed optimized directory: {output_dir}")
    
    print("Cleanup complete.")
    print("\nThank you for using Quill!")
    
    # Exit with error code if there were failures
    if failed or failed_uploads or (uploaded == [] and len(optimized) > 0):
        # If we had processing failures, upload failures, or optimized images but none uploaded
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main() 