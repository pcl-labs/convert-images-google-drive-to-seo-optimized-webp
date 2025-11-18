from __future__ import annotations

import asyncio
import json
from enum import Enum
from typing import Any, Dict, Optional, Union, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, HttpUrl, ConfigDict

from .deps import ensure_db, ensure_services, get_current_user
from .models import (
    GenerateBlogOptions,
    GenerateBlogRequest,
    DocumentVersionDetail,
    DocumentVersionSummary,
    JobProgress,
    JobStatus,
    JobStatusEnum,
)
from .protected import (
    start_ingest_youtube_job,
    start_ingest_text_job,
    start_generate_blog_job,
)
from .database import (
    get_job,
    get_document,
    list_document_versions,
    get_document_version,
)
from .config import settings

MAX_TEXT_LENGTH = 20000


class ContentMode(str, Enum):
    structured = "structured"
    markdown = "markdown"


class ContentFormat(str, Enum):
    json = "json"
    mdx = "mdx"
    html = "html"


class BlogFromYouTubeRequest(BaseModel):
    youtube_url: HttpUrl
    mode: ContentMode = ContentMode.structured
    format: Optional[ContentFormat] = None
    async_request: bool = Field(default=True, alias="async")
    options: GenerateBlogOptions = Field(default_factory=GenerateBlogOptions)
    instructions: Optional[str] = Field(default=None, max_length=2000)

    model_config = ConfigDict(populate_by_name=True)


class ContentJobResponse(BaseModel):
    mode: ContentMode
    format: ContentFormat
    job_id: str
    document_id: str
    status: JobStatusEnum
    job_type: str
    detail: str


class StructuredContentResponse(BaseModel):
    mode: ContentMode = Field(default=ContentMode.structured)
    format: ContentFormat = Field(default=ContentFormat.json)
    document_id: str
    version_id: str
    content: Dict[str, Any]


class MarkdownContentResponse(BaseModel):
    mode: ContentMode = Field(default=ContentMode.markdown)
    format: ContentFormat
    document_id: str
    version_id: str
    body: str


ContentResponse = Union[StructuredContentResponse, MarkdownContentResponse, ContentJobResponse]


class BlogFromTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    title: Optional[str] = Field(default=None, max_length=200)
    mode: ContentMode = ContentMode.structured
    format: Optional[ContentFormat] = None
    async_request: bool = Field(default=True, alias="async")
    options: GenerateBlogOptions = Field(default_factory=GenerateBlogOptions)
    instructions: Optional[str] = Field(default=None, max_length=2000)

    model_config = ConfigDict(populate_by_name=True)

router = APIRouter(prefix="/v1/content", tags=["Content"])
documents_router = APIRouter(prefix="/v1/documents", tags=["Documents"])
jobs_router = APIRouter(prefix="/v1/jobs", tags=["Jobs"])


@router.post("/blog_from_youtube", response_model=ContentResponse)
async def blog_from_youtube(
    payload: BlogFromYouTubeRequest,
    user: dict = Depends(get_current_user),
):
    db, queue = ensure_services()
    user_id = user["user_id"]
    resolved_mode = payload.mode
    resolved_format = _resolve_format(resolved_mode, payload.format)

    options_model = payload.options.model_copy(deep=True)
    if payload.instructions and not options_model.instructions:
        options_model.instructions = payload.instructions
    autopilot_options = options_model.model_dump(exclude_none=True) or None
    manual_pipeline = not payload.async_request and settings.use_inline_queue
    autopilot_enabled = not manual_pipeline

    ingest_status = await start_ingest_youtube_job(
        db,
        queue,
        user_id,
        str(payload.youtube_url),
        autopilot_options=autopilot_options,
        autopilot_enabled=autopilot_enabled,
    )
    document_id = ingest_status.document_id
    if not document_id:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create document for YouTube URL")

    if payload.async_request or not settings.use_inline_queue:
        job_payload = _job_response(
            resolved_mode,
            resolved_format,
            ingest_status,
            document_id,
            job_type=ingest_status.job_type or "ingest_youtube",
            detail="Ingest + autopilot pipeline enqueued. Monitor /api/pipelines/stream for updates.",
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=job_payload.model_dump())

    ingest_row = await _wait_for_job_completion(db, user_id, ingest_status.job_id)
    if ingest_row.get("status") != JobStatusEnum.COMPLETED.value:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="YouTube ingestion did not complete successfully; check job status.",
        )

    blog_status = await start_generate_blog_job(
        db,
        queue,
        user_id,
        GenerateBlogRequest(document_id=document_id, options=options_model),
    )
    blog_row = await _wait_for_job_completion(db, user_id, blog_status.job_id)
    if blog_row.get("status") != JobStatusEnum.COMPLETED.value:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="Blog generation failed; check job status for details.",
        )
    return _build_sync_response(blog_row, resolved_mode, resolved_format, document_id)


@router.post("/blog_from_text", response_model=ContentResponse)
async def blog_from_text(
    payload: BlogFromTextRequest,
    user: dict = Depends(get_current_user),
):
    db, queue = ensure_services()
    user_id = user["user_id"]
    resolved_mode = payload.mode
    resolved_format = _resolve_format(resolved_mode, payload.format)

    options_model = payload.options.model_copy(deep=True)
    if payload.instructions and not options_model.instructions:
        options_model.instructions = payload.instructions

    ingest_status = await start_ingest_text_job(
        db,
        queue,
        user_id,
        payload.text,
        payload.title,
    )
    document_id = ingest_status.document_id
    if not document_id:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create document for provided text")

    blog_status = await start_generate_blog_job(
        db,
        queue,
        user_id,
        GenerateBlogRequest(document_id=document_id, options=options_model),
    )

    if payload.async_request or not settings.use_inline_queue:
        job_payload = _job_response(
            resolved_mode,
            resolved_format,
            blog_status,
            document_id,
            job_type=blog_status.job_type or "generate_blog",
            detail="Text ingestion + blog generation enqueued.",
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=job_payload.model_dump())

    blog_row = await _wait_for_job_completion(db, user_id, blog_status.job_id)
    if blog_row.get("status") != JobStatusEnum.COMPLETED.value:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="Blog generation failed; check job status for details.",
        )
    return _build_sync_response(blog_row, resolved_mode, resolved_format, document_id)


class BlogFromDocumentRequest(BaseModel):
    document_id: str = Field(..., min_length=5, max_length=100)
    mode: ContentMode = ContentMode.structured
    format: Optional[ContentFormat] = None
    async_request: bool = Field(default=True, alias="async")
    options: GenerateBlogOptions = Field(default_factory=GenerateBlogOptions)
    instructions: Optional[str] = Field(default=None, max_length=2000)

    model_config = ConfigDict(populate_by_name=True)


@router.post("/blog_from_document", response_model=ContentResponse)
async def blog_from_document(
    payload: BlogFromDocumentRequest,
    user: dict = Depends(get_current_user),
):
    db, queue = ensure_services()
    user_id = user["user_id"]
    resolved_mode = payload.mode
    resolved_format = _resolve_format(resolved_mode, payload.format)

    options_model = payload.options.model_copy(deep=True)
    if payload.instructions and not options_model.instructions:
        options_model.instructions = payload.instructions

    job_status = await start_generate_blog_job(
        db,
        queue,
        user_id,
        GenerateBlogRequest(document_id=payload.document_id, options=options_model),
    )

    if payload.async_request or not settings.use_inline_queue:
        job_payload = _job_response(
            resolved_mode,
            resolved_format,
            job_status,
            payload.document_id,
            job_type=job_status.job_type or "generate_blog",
            detail="Blog generation job queued.",
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=job_payload.model_dump())

    job_row = await _wait_for_job_completion(db, user_id, job_status.job_id)
    if job_row.get("status") != JobStatusEnum.COMPLETED.value:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="Blog generation failed; check job status for details.",
        )
    return _build_sync_response(job_row, resolved_mode, resolved_format, payload.document_id)


class DocumentOverviewResponse(BaseModel):
    document: Dict[str, Any]
    latest_version: Optional[DocumentVersionSummary] = None


@documents_router.get("/{document_id}", response_model=DocumentOverviewResponse)
async def get_document_overview(document_id: str, include_latest: bool = Query(True), user: dict = Depends(get_current_user)):
    db = ensure_db()
    doc_row = await get_document(db, document_id, user["user_id"])
    if not doc_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
    document = _document_summary(doc_row)
    latest_version = None
    if include_latest:
        versions = await list_document_versions(db, document_id, user["user_id"], limit=1)
        if versions:
            latest_version = _version_summary_model(versions[0])
    return DocumentOverviewResponse(document=document, latest_version=latest_version)


@documents_router.get("/{document_id}/versions/{version_id}")
async def get_document_version_v1(
    document_id: str,
    version_id: str,
    mode: ContentMode = ContentMode.structured,
    format: Optional[ContentFormat] = None,
    user: dict = Depends(get_current_user),
):
    db = ensure_db()
    row = await get_document_version(db, document_id, version_id, user["user_id"])
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Version not found")
    detail = _version_detail_model(row)
    return _document_version_response(detail, mode, format)


@documents_router.get("/{document_id}/versions/latest")
async def get_document_latest_version(
    document_id: str,
    mode: ContentMode = ContentMode.structured,
    format: Optional[ContentFormat] = None,
    user: dict = Depends(get_current_user),
):
    db = ensure_db()
    versions = await list_document_versions(db, document_id, user["user_id"], limit=1)
    if not versions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No versions found for document")
    detail = _version_detail_model(versions[0])
    return _document_version_response(detail, mode, format)


class JobDetailResponse(BaseModel):
    job: JobStatus
    links: Dict[str, str] = Field(default_factory=dict)


@jobs_router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job_detail(job_id: str, user: dict = Depends(get_current_user)):
    db = ensure_db()
    job_row = await get_job(db, job_id, user["user_id"])
    if not job_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    job_model = _job_status_from_row(job_row)
    links = _build_job_links(job_model)
    return JobDetailResponse(job=job_model, links=links)

def _job_response(
    mode: ContentMode,
    fmt: ContentFormat,
    job_status: JobStatus,
    document_id: str,
    *,
    job_type: str,
    detail: str,
) -> ContentJobResponse:
    return ContentJobResponse(
        mode=mode,
        format=fmt,
        job_id=job_status.job_id,
        document_id=document_id,
        status=job_status.status,
        job_type=job_type,
        detail=detail,
    )


def _document_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    doc = dict(row)
    doc["metadata"] = _json_field(doc.get("metadata"), {})
    doc["frontmatter"] = _json_field(doc.get("frontmatter"), {})
    doc.pop("raw_text", None)
    return doc


def _version_summary_model(row: Dict[str, Any]) -> DocumentVersionSummary:
    return DocumentVersionSummary(
        version_id=row.get("version_id"),
        document_id=row.get("document_id"),
        version=row.get("version") or 0,
        content_format=row.get("content_format") or "unknown",
        frontmatter=_json_field(row.get("frontmatter"), {}),
        created_at=row.get("created_at"),
    )


def _version_detail_model(row: Dict[str, Any]) -> DocumentVersionDetail:
    return DocumentVersionDetail(
        version_id=row.get("version_id"),
        document_id=row.get("document_id"),
        version=row.get("version") or 0,
        content_format=row.get("content_format") or "unknown",
        frontmatter=_json_field(row.get("frontmatter"), {}),
        body_mdx=row.get("body_mdx"),
        body_html=row.get("body_html"),
        outline=_json_field(row.get("outline"), []),
        chapters=_json_field(row.get("chapters"), []),
        sections=_json_field(row.get("sections"), []),
        assets=_json_field(row.get("assets"), {}),
        created_at=row.get("created_at"),
    )


def _document_version_response(
    detail: DocumentVersionDetail,
    mode: ContentMode,
    fmt: Optional[ContentFormat],
):
    resolved_format = _resolve_format(mode, fmt)
    if mode == ContentMode.structured:
        if resolved_format != ContentFormat.json:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Structured mode only supports JSON format")
        return detail
    if resolved_format == ContentFormat.mdx:
        body = detail.body_mdx
        media_type = "text/plain; charset=utf-8"
    else:
        body = detail.body_html
        media_type = "text/html; charset=utf-8"
    if not body:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{resolved_format.value.upper()} body unavailable for this version")
    return PlainTextResponse(body, media_type=media_type)


def _json_field(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, (dict, list)) else default
        except json.JSONDecodeError:
            return default
    if isinstance(value, (dict, list)):
        return value
    return default


def _job_status_from_row(row: Dict[str, Any]) -> JobStatus:
    progress_raw = row.get("progress") or "{}"
    try:
        progress_data = json.loads(progress_raw)
    except json.JSONDecodeError:
        progress_data = {}
    if not isinstance(progress_data, dict):
        progress_data = {}
    progress = JobProgress(**{"stage": progress_data.get("stage", "initializing"), **progress_data})
    output = _safe_json(row.get("output"))
    return JobStatus(
        job_id=row.get("job_id"),
        user_id=row.get("user_id"),
        status=JobStatusEnum(row.get("status")),
        progress=progress,
        created_at=row.get("created_at"),
        completed_at=row.get("completed_at"),
        error=row.get("error"),
        job_type=row.get("job_type"),
        document_id=row.get("document_id"),
        output=output if isinstance(output, dict) else None,
    )


def _build_job_links(job: JobStatus) -> Dict[str, str]:
    links: Dict[str, str] = {}
    if job.document_id:
        links["document"] = f"/v1/documents/{job.document_id}"
        links["latest_version"] = f"/v1/documents/{job.document_id}/versions/latest"
        if job.output and job.output.get("version_id"):
            links["version"] = f"/v1/documents/{job.document_id}/versions/{job.output['version_id']}"
    return links


def _safe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


async def _wait_for_job_completion(db, user_id: str, job_id: str, timeout: float = 120.0) -> Dict[str, Any]:
    """Poll the jobs table until completion or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        job_row = await get_job(db, job_id, user_id)
        if job_row and job_row.get("status") in {
            JobStatusEnum.COMPLETED.value,
            JobStatusEnum.FAILED.value,
            JobStatusEnum.CANCELLED.value,
        }:
            return dict(job_row)
        if asyncio.get_event_loop().time() >= deadline:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Job {job_id} did not complete within {int(timeout)} seconds",
            )
        await asyncio.sleep(0.5)


def _resolve_format(mode: ContentMode, requested: Optional[ContentFormat]) -> ContentFormat:
    if requested:
        return requested
    return ContentFormat.json if mode == ContentMode.structured else ContentFormat.mdx


def _build_sync_response(
    job_row: Dict[str, Any],
    mode: ContentMode,
    fmt: ContentFormat,
    document_id: str,
) -> ContentResponse:
    output = job_row.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            output = {}
    if not isinstance(output, dict):
        output = {}
    version_id = output.get("version_id")
    if not version_id:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Job output missing version information")

    if mode == ContentMode.structured:
        if fmt != ContentFormat.json:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Structured mode only supports JSON format")
        return StructuredContentResponse(
            document_id=document_id,
            version_id=version_id,
            content=output,
        )

    if fmt == ContentFormat.json:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Markdown mode requires mdx or html format")
    body_block = output.get("body") or {}
    if fmt == ContentFormat.mdx:
        body = body_block.get("mdx")
    else:
        body = body_block.get("html")
    if not body:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Blog output missing {fmt.value.upper()} body",
        )
    return MarkdownContentResponse(
        format=fmt,
        document_id=document_id,
        version_id=version_id,
        body=body,
    )
