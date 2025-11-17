"""
Extension normalization utilities.
"""
import os
from typing import Iterable, Set, List, Optional

def normalize_extensions(exts: Iterable[str]) -> Set[str]:
    """Normalize extensions to lowercase with leading dots.
    e.g., ['jpg', '.PNG'] -> {'.jpg', '.png'}
    Strips whitespace, skips empty strings, and filters out single-dot entries.
    """
    result = set()
    for e in exts:
        # Strip whitespace from each extension
        e = e.strip()
        # Skip empty strings after stripping
        if not e:
            continue
        # Filter out single-dot entries (like '.' or ' . ')
        if e == '.':
            continue
        # Lowercase and ensure leading dot
        normalized = e.lower() if e.startswith('.') else f'.{e.lower()}'
        result.add(normalized)
    return result


def detect_extensions_in_dir(directory: str, fallback: Optional[List[str]] = None) -> List[str]:
    """
    Detect file extensions present in a directory.
    
    Args:
        directory: Path to directory to scan
        fallback: Default extensions to return if directory is empty or doesn't exist.
                 Defaults to ['.webp', '.jpg', '.png', '.avif']
    
    Returns:
        List of normalized extensions (with leading dots) found in directory,
        or fallback list if no files found.
    """
    if fallback is None:
        fallback = ['.webp', '.jpg', '.png', '.avif']
    
    # Sort fallback once for consistent ordering across all return paths
    sorted_fallback = sorted(fallback)
    
    if not os.path.exists(directory) or not os.path.isdir(directory):
        return sorted_fallback
    
    extensions = set()
    try:
        for filename in os.listdir(directory):
            if os.path.isfile(os.path.join(directory, filename)):
                _, ext = os.path.splitext(filename)
                if ext:  # Only add non-empty extensions
                    extensions.add(ext.lower())
    except (OSError, PermissionError):
        # If we can't read the directory, return fallback
        return sorted_fallback
    
    if not extensions:
        return sorted_fallback
    
    # Return as sorted list for consistency
    return sorted(list(extensions))
