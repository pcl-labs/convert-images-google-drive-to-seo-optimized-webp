"""
Simple static file serving for Cloudflare Workers.

Uses filesystem-based StaticFiles mount.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from fastapi.staticfiles import StaticFiles


def mount_static_files(app, static_dir_setting: Optional[str] = None) -> None:
    """
    Mount static files on the FastAPI app.
    
    For now, always use the filesystem path relative to this file:
    src/workers/static
    
    Args:
        app: FastAPI application instance
        static_dir_setting: Optional filesystem path to static directory (for local dev)
    """
    base_dir = Path(__file__).resolve().parent.parent  # src/workers
    static_dir = base_dir / "static"
    
    # Prefer explicit static_dir_setting if it's a valid directory
    if static_dir_setting:
        candidate = Path(static_dir_setting).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        candidate = candidate.resolve()
        if candidate.is_dir():
            static_dir = candidate
    
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    else:
        # If it doesn't exist, just don't mount; /static/* will 404
        # (But it should exist in our project)
        pass

