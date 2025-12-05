from fastapi import APIRouter, Request, HTTPException, status, Form
from fastapi.responses import JSONResponse, RedirectResponse, Response, PlainTextResponse
from typing import Optional
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from .config import settings
from .utils import is_secure_request
from .deps import get_queue_producer
from .app_logging import get_logger
from .models import JobType
from .simple_http import AsyncSimpleClient, HTTPStatusError, RequestError

router = APIRouter()

logger = get_logger(__name__)


# Removed legacy OAuth login endpoints - authentication is now handled by Better Auth


@router.get("/api", tags=["Public"])
async def root():
    # Determine queue mode for debugging
    queue_mode = "inline" if settings.use_inline_queue else ("workers-binding" if settings.queue else ("api" if settings.cloudflare_api_token else "none"))
    
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "queue_mode": queue_mode,
        "endpoints": {
            "jobs": "/api/v1/jobs",
            "health": "/health",
            "docs": "/docs",
        },
    }


@router.get("/robots.txt", response_class=PlainTextResponse, tags=["Public"])
async def robots_txt(request: Request) -> str:
    base = (settings.base_url or "").strip().rstrip("/")
    if not base:
        base = f"{request.url.scheme}://{request.url.netloc}"
    return f"""User-agent: *
Allow: /

Sitemap: {base}/sitemap.xml
"""


@router.get("/health", tags=["Public"]) 
async def health():
    # Minimal health; deeper checks can live in protected ops if needed
    return {
        "status": "healthy",
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc),
    }


@router.get("/auth/logout", tags=["Authentication"], include_in_schema=False)
async def logout_get(request: Request):
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="Browser-based authentication is no longer supported")
