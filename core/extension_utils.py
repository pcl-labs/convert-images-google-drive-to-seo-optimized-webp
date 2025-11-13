"""
Extension normalization utilities.
"""
from typing import Iterable, Set

def normalize_extensions(exts: Iterable[str]) -> Set[str]:
    """Normalize extensions to lowercase with leading dots.
    e.g., ['jpg', '.PNG'] -> {'.jpg', '.png'}
    """
    return set((e.lower() if e.startswith('.') else f'.{e.lower()}') for e in exts)
