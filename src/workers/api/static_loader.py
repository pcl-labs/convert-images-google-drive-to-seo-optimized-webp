"""
Simple static file serving for Cloudflare Workers.

Instead of Starlette StaticFiles (which is misbehaving in Workers),
we serve files directly from src/workers/static via a route.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Any
import logging

from fastapi import HTTPException, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)

# We'll compute BASE_DIR the same way templates do, but at runtime
# to avoid import order issues


def mount_static_files(app, static_dir_setting: Optional[str] = None, assets_binding: Optional[Any] = None) -> None:
    """
    Mount static files on the FastAPI app.

    Instead of Starlette StaticFiles (which is misbehaving in Workers),
    we serve files directly from src/workers/static via a route.

    Args:
        app: FastAPI application instance
        static_dir_setting: Optional filesystem path to static directory (for local dev)
        assets_binding: Optional Cloudflare Assets binding (env.ASSETS) for Workers runtime
    """
    # Use the same path resolution pattern as templates (which work in Workers)
    # Templates use: BASE_DIR = Path(__file__).resolve().parent.parent
    # But we compute it from this module's location
    base_dir = Path(__file__).resolve().parent.parent  # src/workers
    static_dir = base_dir / "static"
    
    # Respect static_dir_setting if it exists
    if static_dir_setting:
        candidate = Path(static_dir_setting).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        candidate = candidate.resolve()
        if candidate.is_dir():
            static_dir = candidate
    
    # Store the resolved static_dir and assets binding on app state
    app.state.static_dir = static_dir
    app.state.assets_binding = assets_binding
    logger.info(f"Static files directory set to: {static_dir} (exists: {static_dir.exists()}, is_dir: {static_dir.is_dir()})")
    if assets_binding:
        logger.info("Using Cloudflare Assets binding for static files")
    
    @app.get("/static/{path:path}", name="static")
    async def serve_static(path: str, request: Request):
        # basic security
        if ".." in path:
            raise HTTPException(status_code=403, detail="Invalid path")
        
        # Use Cloudflare Assets binding (Workers runtime)
        if not app.state.assets_binding:
            raise HTTPException(status_code=500, detail="Assets binding not available")
        
        try:
            # Assets binding's fetch() expects a URL string or Request-like object
            # The path should be relative to the assets directory root
            # For /static/css/app.css, the assets binding expects /css/app.css
            asset_path = f"/{path}" if not path.startswith("/") else path
            asset_url = f"https://example.com{asset_path}"
            
            logger.info(f"Attempting to fetch from assets binding: {asset_path} (URL: {asset_url})")
            # Try passing the URL string directly
            response = await app.state.assets_binding.fetch(asset_url)
            
            if not response:
                raise HTTPException(status_code=500, detail="Assets binding returned no response")
            
            if response.status == 200:
                content = await response.bytes()
                logger.info(f"Successfully fetched {path} from assets binding ({len(content)} bytes)")
                # Determine content type from extension
                content_type = "application/octet-stream"
                if path.endswith('.css'):
                    content_type = "text/css"
                elif path.endswith('.js'):
                    content_type = "application/javascript"
                elif path.endswith('.png'):
                    content_type = "image/png"
                elif path.endswith('.jpg') or path.endswith('.jpeg'):
                    content_type = "image/jpeg"
                elif path.endswith('.svg'):
                    content_type = "image/svg+xml"
                elif path.endswith('.webmanifest'):
                    content_type = "application/manifest+json"
                return Response(content=content, media_type=content_type)
            else:
                raise HTTPException(status_code=response.status, detail=f"Assets binding returned status {response.status} for {path}")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Assets binding fetch failed for {path}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error fetching from assets binding: {str(e)}")

