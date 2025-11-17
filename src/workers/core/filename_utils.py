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
    # Normalize whitespace and case first
    cleaned = folder_name.strip().lower()
    # Replace invalid characters with '-'
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", cleaned)
    # Strip leading/trailing dashes that can result from replacement
    cleaned = cleaned.strip("-")
    if not cleaned:
        raise ValueError("Folder name is empty or invalid after sanitization")
    return cleaned


def compose_download_name(name: str, file_id: str, ext: str, sep: str = FILENAME_ID_SEPARATOR) -> str:
    """Compose filename using `<name><sep><file_id>.<ext>`.
    `ext` may be with or without a leading dot.

    `file_id` must be an identifier that does not contain the separator
    and is composed of safe characters for filenames.
    """
    # Validate name is non-empty and does not contain separator
    if not name:
        raise ValueError(f"name must be non-empty (separator: {sep!r})")
    if sep in name:
        raise ValueError(f"name must not contain separator {sep!r}, got: {name!r}")
    
    # Build a file_id pattern that excludes the configured separator when
    # the separator is one of the characters in the base pattern.
    base_pattern = r'^[A-Za-z0-9_-]+$'
    if sep == "_":
        file_id_pattern = r'^[A-Za-z0-9-]+$'
    elif sep == "-":
        file_id_pattern = r'^[A-Za-z0-9_]+$'
    else:
        file_id_pattern = base_pattern

    if not re.match(file_id_pattern, file_id):
        raise ValueError(f"file_id must match pattern {file_id_pattern!r}, got: {file_id!r}")
    
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
