"""
FastAPI entrypoint for local development (Uvicorn) and shared imports.
"""

from .app_factory import create_app

app = create_app()

__all__ = ["app"]
