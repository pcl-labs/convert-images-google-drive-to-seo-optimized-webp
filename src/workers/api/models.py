"""
Pydantic models for request/response validation.
"""

from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
from pydantic import HttpUrl
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime, timezone


class JobStatusEnum(str, Enum):
    """Job status enumeration."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProjectStatusEnum(str, Enum):
    """Project status enumeration for YouTube-backed projects."""

    PENDING = "pending"
    TRANSCRIPT_READY = "transcript_ready"
    EMBEDDED = "embedded"
    BLOG_GENERATED = "blog_generated"
    FAILED = "failed"


class JobType(str, Enum):
    """Job type classification for pipelines and ingestion."""
    OPTIMIZE_DRIVE = "optimize_drive"
    INGEST_YOUTUBE = "ingest_youtube"
    INGEST_TEXT = "ingest_text"
    INGEST_DRIVE = "ingest_drive"
    DRIVE_CHANGE_POLL = "drive_change_poll"
    DRIVE_WATCH_RENEWAL = "drive_watch_renewal"
    INGEST_DRIVE_FOLDER = "ingest_drive_folder"
    GENERATE_BLOG = "generate_blog"


class JobProgress(BaseModel):
    """Job progress tracking."""
    
    stage: str = Field(default="initializing", description="Current processing stage")
    downloaded: int = Field(default=0, ge=0)
    optimized: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    uploaded: int = Field(default=0, ge=0)
    deleted: int = Field(default=0, ge=0)
    download_failed: int = Field(default=0, ge=0)
    upload_failed: int = Field(default=0, ge=0)
    recent_logs: List[str] = Field(default_factory=list, max_length=50, exclude=True)


class JobStatus(BaseModel):
    """Job status response model."""
    
    job_id: str
    user_id: str
    status: JobStatusEnum
    progress: JobProgress
    created_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    job_type: Optional[str] = None
    document_id: Optional[str] = None
    output: Optional[Dict[str, Any]] = None
    
    model_config = ConfigDict(use_enum_values=True)


class JobListResponse(BaseModel):
    """Paginated job list response."""
    
    jobs: List[JobStatus]
    total: int
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    has_more: bool


class UserResponse(BaseModel):
    """User information response."""
    
    user_id: str
    github_id: Optional[str] = None
    email: Optional[str] = None
    created_at: datetime


class APIKeyResponse(BaseModel):
    """API key generation response."""
    
    api_key: str
    created_at: datetime
    message: str = "Store this API key securely. It will not be shown again."


class ErrorResponse(BaseModel):
    """Standard error response."""
    
    error: str
    error_code: str
    detail: Optional[str] = None
    request_id: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response."""
    
    status: str
    version: str
    database: Optional[str] = None
    queue: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StatsResponse(BaseModel):
    """API statistics response."""
    
    total_jobs: int
    completed_jobs: int
    failed_jobs: int
    pending_jobs: int
    processing_jobs: int
    total_users: Optional[int] = None


class Document(BaseModel):
    document_id: str
    user_id: str
    source_type: str
    source_ref: Optional[str] = None
    raw_text: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    content_format: Optional[str] = None
    frontmatter: Optional[Dict[str, Any]] = None
    latest_version_id: Optional[str] = None
    drive_file_id: Optional[str] = None
    drive_revision_id: Optional[str] = None
    drive_folder_id: Optional[str] = None
    drive_drafts_folder_id: Optional[str] = None
    drive_media_folder_id: Optional[str] = None
    drive_published_folder_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DriveDocumentRequest(BaseModel):
    drive_source: str = Field(..., min_length=5, max_length=500, description="Drive share link or folder ID")


class OptimizeDocumentRequest(BaseModel):
    document_id: str = Field(..., min_length=5, max_length=100)
    extensions: Optional[List[str]] = Field(
        default=["jpg", "jpeg", "png", "bmp", "tiff", "heic", "webp"],
        description="List of image extensions to process",
        max_length=10
    )
    overwrite: bool = Field(default=False)
    skip_existing: bool = Field(default=True)
    cleanup_originals: bool = Field(default=False)
    max_retries: int = Field(default=3, ge=0, le=10)

    @field_validator('extensions')
    @classmethod
    def validate_extensions(cls, v):
        allowed = {'jpg', 'jpeg', 'png', 'bmp', 'tiff', 'tif', 'heic', 'webp'}
        if not v:
            return ["jpg", "jpeg", "png", "bmp", "tiff", "heic", "webp"]
        validated = []
        invalid = []
        for ext in v:
            ext_clean = str(ext).lower().lstrip('.')
            if ext_clean in allowed:
                validated.append(ext_clean)
            else:
                invalid.append(ext)
        if invalid:
            raise ValueError(f"Invalid extensions: {invalid}. Allowed: {sorted(allowed)}")
        return validated

    @model_validator(mode="after")
    def validate_flags(self):
        if self.overwrite and self.skip_existing:
            raise ValueError("'overwrite' and 'skip_existing' cannot both be True")
        return self


class GenerateBlogOptions(BaseModel):
    tone: Optional[str] = Field(default=None, min_length=3, max_length=40)
    max_sections: Optional[int] = Field(default=None, ge=1, le=12)
    target_chapters: Optional[int] = Field(default=None, ge=1, le=12)
    include_images: Optional[bool] = Field(default=None)
    model: Optional[str] = Field(default=None, min_length=3, max_length=60)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    section_index: Optional[int] = Field(default=None, ge=0, le=50)
    content_type: Optional[str] = Field(
        default=None,
        description="Optional content type hint such as 'generic_blog', 'faq', 'recipe'.",
        min_length=3,
        max_length=80,
    )
    instructions: Optional[str] = Field(
        default=None,
        description="Additional generation instructions or constraints.",
        min_length=1,
        max_length=2000,
    )

    @model_validator(mode="after")
    def validate_section_index_bounds(self):
        if self.section_index is not None and self.max_sections is not None:
            if self.section_index < 0 or self.section_index >= self.max_sections:
                raise ValueError("section_index must be >= 0 and < max_sections")
        return self


class GenerateBlogRequest(BaseModel):
    document_id: str = Field(..., min_length=5, max_length=100)
    options: GenerateBlogOptions = Field(default_factory=GenerateBlogOptions)


class DocumentVersionSummary(BaseModel):
    version_id: str
    document_id: str
    version: int = Field(ge=0)
    content_format: str
    frontmatter: Optional[Dict[str, Any]] = None
    created_at: datetime


class DocumentVersionDetail(DocumentVersionSummary):
    body_mdx: Optional[str] = None
    body_html: Optional[str] = None
    outline: Optional[List[Dict[str, Any]]] = None
    chapters: Optional[List[Dict[str, Any]]] = None
    sections: Optional[List[Dict[str, Any]]] = None
    assets: Optional[Dict[str, Any]] = None


class DocumentVersionList(BaseModel):
    versions: List[DocumentVersionSummary]


class ExportTarget(str, Enum):
    google_docs = "google_docs"
    zapier = "zapier"
    wordpress = "wordpress"


class DocumentExportRequest(BaseModel):
    target: ExportTarget
    version_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class DocumentExportResponse(BaseModel):
    export_id: str
    status: str
    target: ExportTarget
    version_id: str
    document_id: str
    created_at: datetime


class IngestYouTubeRequest(BaseModel):
    url: HttpUrl

    @field_validator("url")
    @classmethod
    def validate_youtube_host(cls, v: HttpUrl) -> HttpUrl:
        host = (v.host or "").lower()
        if not (
            host == "youtube.com" or
            host.endswith(".youtube.com") or
            host == "youtu.be"
        ):
            raise ValueError("URL must be a YouTube URL (youtube.com or youtu.be)")
        return v


class IngestTextRequest(BaseModel):
    text: str = Field(..., max_length=20000)
    title: Optional[str] = Field(default=None, max_length=500)


class IngestDriveRequest(BaseModel):
    document_id: str = Field(..., min_length=5, max_length=100)


class DriveChangePollRequest(BaseModel):
    document_ids: Optional[List[str]] = None


class Project(BaseModel):
    project_id: str
    document_id: str
    user_id: str
    youtube_url: Optional[str]
    status: ProjectStatusEnum
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(use_enum_values=True)


class CreateProjectRequest(BaseModel):
    youtube_url: HttpUrl

    @field_validator("youtube_url")
    @classmethod
    def validate_youtube_host(cls, v: HttpUrl) -> HttpUrl:
        # Reuse the same host restrictions as IngestYouTubeRequest so project
        # creation and ingest share URL validation.
        host = (v.host or "").lower()
        if not (
            host == "youtube.com" or
            host.endswith(".youtube.com") or
            host == "youtu.be"
        ):
            raise ValueError("youtube_url must be a YouTube URL (youtube.com or youtu.be)")
        return v


class ProjectResponse(BaseModel):
    project: Project
    document: Optional[Document] = None


class TranscriptChunk(BaseModel):
    chunk_id: str
    chunk_index: int = Field(ge=0)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    text_preview: str

    @model_validator(mode="after")
    def validate_char_range(self):
        if self.start_char >= self.end_char:
            raise ValueError("start_char must be strictly less than end_char for a transcript chunk")
        return self


class TranscriptResponse(BaseModel):
    project_id: str
    text: str
    chunks: List[TranscriptChunk]
    metadata: Optional[Dict[str, Any]] = None


class ChunkAndEmbedResponse(BaseModel):
    project_id: str
    chunks_created: int
    embeddings_stored: int
    status: str


class TranscriptSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(5, ge=1, le=20)


class TranscriptSearchMatch(BaseModel):
    chunk_id: str
    chunk_index: int = Field(ge=0)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    text_preview: str
    score: Optional[float] = None

    @model_validator(mode="after")
    def validate_char_range(self):
        if self.start_char >= self.end_char:
            raise ValueError("start_char must be strictly less than end_char for a search match")
        return self


class TranscriptSearchResponse(BaseModel):
    project_id: str
    query: str
    matches: List[TranscriptSearchMatch]


class ProjectGenerateBlogRequest(BaseModel):
    options: Optional[GenerateBlogOptions] = None


class ProjectBlog(BaseModel):
    project_id: str
    document_id: str
    version_id: str
    status: ProjectStatusEnum
    frontmatter: Optional[Dict[str, Any]] = None
    body_mdx: Optional[str] = None
    outline: Optional[Any] = None
    created_at: datetime

    model_config = ConfigDict(use_enum_values=True)


class GenerateProjectBlogResponse(BaseModel):
    job_id: Optional[str] = None
    blog: Optional[ProjectBlog] = None
    project: Project


class ProjectSectionSummary(BaseModel):
    section_id: str
    index: int = Field(ge=0)
    title: Optional[str] = None
    word_count: Optional[int] = Field(default=None, ge=0)


class ProjectSectionListResponse(BaseModel):
    project_id: str
    document_id: str
    version_id: str
    sections: List[ProjectSectionSummary]


class ProjectSectionDetail(BaseModel):
    section_id: str
    index: int = Field(ge=0)
    title: Optional[str] = None
    body_mdx: str = Field(..., max_length=20000)


class PatchSectionRequest(BaseModel):
    section_id: str
    instructions: str = Field(..., min_length=1, max_length=2000)


class PatchSectionResponse(BaseModel):
    project_id: str
    document_id: str
    version_id: str
    section: ProjectSectionDetail


class ProjectVersionSummary(BaseModel):
    version_id: str
    version: int = Field(ge=0)
    created_at: datetime
    source: Optional[str] = None
    title: Optional[str] = None


class ProjectVersionsResponse(BaseModel):
    project_id: str
    document_id: str
    versions: List[ProjectVersionSummary]


class ProjectVersionDetail(BaseModel):
    project_id: str
    document_id: str
    version_id: str
    version: int
    created_at: datetime
    frontmatter: Optional[Dict[str, Any]] = None
    body_mdx: Optional[str] = None
    outline: Optional[Any] = None
    sections: Optional[Any] = None


class ProjectBlogDiff(BaseModel):
    project_id: str
    document_id: str
    from_version_id: str
    to_version_id: str
    changed_sections: List[str]
    diff_body_mdx: Optional[str] = None
