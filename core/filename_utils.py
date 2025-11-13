"""
Filename and identifier utilities shared across modules.
"""

import os
import re
from .constants import OUTPUT_DIR_PREFIX
from typing import Optional, Tuple

# Separator used between filename and file_id in downloaded files
# Format: "<name><FILENAME_ID_SEPARATOR><file_id>.<ext>"
FILENAME_ID_SEPARATOR = "_"


def sanitize_folder_name(folder_name: str) -> str:
    """Sanitize folder name for use in filenames.
    Replaces non [a-zA-Z0-9_-] characters with '-'.
    """
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", folder_name.strip().lower())


def compose_download_name(name: str, file_id: str, ext: str, sep: str = FILENAME_ID_SEPARATOR) -> str:
    """Compose filename using `<name><sep><file_id>.<ext>`.
    `ext` may be with or without a leading dot.
    """
    # Validate file_id matches the expected pattern
    if not re.match(r"^[A-Za-z0-9_-]+$", file_id):
        raise ValueError(f"file_id must match pattern [A-Za-z0-9_-]+, got: {file_id!r}")
    
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"{name}{sep}{file_id}{ext}"


def parse_download_name(filename: str, sep: str = FILENAME_ID_SEPARATOR) -> Optional[Tuple[str, str, str]]:
    """Parse `<name><sep><file_id>.<ext>` pattern.
    Returns (name, file_id, ext) or None if not matched.
    """
    base = os.path.basename(filename)
    match = re.match(rf"^(.+){re.escape(sep)}([A-Za-z0-9_-]+)\.(\w+)$", base)
    if not match:
        return None
    name, file_id, ext = match.groups()
    return name, file_id, ext


def make_output_dir_name(folder_name: str) -> str:
    """Build the output directory name based on sanitized folder name."""
    clean = sanitize_folder_name(folder_name)
    return f"{OUTPUT_DIR_PREFIX}{clean}"
