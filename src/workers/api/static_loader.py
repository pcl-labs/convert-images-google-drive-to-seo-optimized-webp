"""
Package-based static file serving for Cloudflare Workers.

This module provides static file serving that works in both local dev and
Cloudflare Workers (Pyodide) environments by loading files from the package
using importlib.resources.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Optional
from fastapi import Request, HTTPException, status
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route


def _get_package_static_path() -> Optional[Path]:
    """
    Get the filesystem path to the package static directory if available.
    
    This works in local dev when the package is installed in development mode
    or when running from source.
    
    Returns:
        Path to static directory if found, None otherwise
    """
    try:
        # Try to get the package location from importlib
        import importlib.util
        spec = importlib.util.find_spec("workers")
        if spec and spec.origin:
            # Get the package directory
            package_dir = Path(spec.origin).parent
            static_path = package_dir / "static"
            if static_path.exists() and static_path.is_dir():
                return static_path
    except (AttributeError, ValueError, TypeError):
        pass
    return None


def _read_static_file(relative_path: str) -> Optional[bytes]:
    """
    Read a static file from package resources as bytes.
    
    Args:
        relative_path: Path relative to the static directory (e.g., "css/app.css")
        
    Returns:
        File content as bytes, or None if not found
    """
    try:
        # Use importlib.resources to read from package
        package_ref = importlib.resources.files("workers")
        static_ref = package_ref.joinpath("static")
        file_ref = static_ref.joinpath(relative_path)
        
        # Check if file exists and read it
        if file_ref.is_file():
            return file_ref.read_bytes()
    except (ModuleNotFoundError, ValueError, AttributeError, FileNotFoundError, OSError):
        pass
    return None


def _get_content_type(path: str) -> str:
    """Determine content type from file extension."""
    ext = Path(path).suffix.lower()
    content_types = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".webmanifest": "application/manifest+json",
        ".txt": "text/plain",
        ".html": "text/html",
    }
    return content_types.get(ext, "application/octet-stream")


async def _serve_static_file(request: Request) -> Response:
    """
    ASGI handler for serving static files from package resources.
    
    This handler is used when StaticFiles doesn't work (e.g., in Workers).
    """
    # Extract the path from the request
    path = request.path_params.get("path", "")
    if not path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    
    # Security: prevent directory traversal
    if ".." in path or path.startswith("/"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid path")
    
    # Try to read the file from package resources
    content = _read_static_file(path)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    
    # Determine content type
    content_type = _get_content_type(path)
    
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=3600",  # Cache for 1 hour
        },
    )


def mount_static_files(app, static_dir_setting: Optional[str] = None) -> None:
    """
    Mount static files on the FastAPI app.
    
    This function tries to use StaticFiles (filesystem-based) for local dev,
    but falls back to a custom ASGI router for package-based serving in Workers.
    
    Args:
        app: FastAPI application instance
        static_dir_setting: Optional filesystem path to static directory (for local dev)
    """
    from .config import settings
    
    # Try filesystem-based approach first (for local dev)
    if static_dir_setting:
        static_path = Path(static_dir_setting).expanduser()
        if not static_path.is_absolute():
            static_path = Path.cwd() / static_path
        static_path = static_path.resolve()
        
        if static_path.exists() and static_path.is_dir():
            # Use standard StaticFiles for local dev
            app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
            return
    
    # Fallback: Try to find package static directory on filesystem (local dev)
    package_static_path = _get_package_static_path()
    if package_static_path:
        # Use StaticFiles with package path (works in local dev)
        app.mount("/static", StaticFiles(directory=str(package_static_path)), name="static")
        return
    
    # Final fallback: Use custom ASGI router for package resources (Works workers)
    # This handles the case where we can't access filesystem but can read from package
    from starlette.routing import Mount
    
    app.mount(
        "/static",
        Mount(
            routes=[
                Route("/{path:path}", _serve_static_file, methods=["GET"]),
            ],
            name="static",
        ),
    )

