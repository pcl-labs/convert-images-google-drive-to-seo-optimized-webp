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
    session_id: Optional[str] = None
    
    model_config = ConfigDict(use_enum_values=True)


class PipelineEvent(BaseModel):
    sequence: int
    event_id: str
    user_id: str
    job_id: str
    event_type: str
    stage: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    created_at: datetime
    session_id: Optional[str] = None


class SessionEventsResponse(BaseModel):
    session_id: str
    events: List[PipelineEvent]
    jobs: List[JobStatus]


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


# Removed: Document, DriveDocumentStatus, DrivePublishRequest, DriveDocumentRequest, 
# DriveWorkspaceLinkRequest, OptimizeDocumentRequest - Documents feature removed

# Note: GenerateBlogOptions kept for Project blog generation (YouTube-related)
# Removed: GenerateBlogRequest - Standalone blog generation removed (documents feature)


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
    schema_type: Optional[str] = Field(
        default=None,
        description="Optional schema.org content type identifier (e.g. https://schema.org/FAQPage).",
        min_length=5,
        max_length=200,
    )

    @model_validator(mode="after")
    def validate_section_index_bounds(self):
        if self.section_index is not None and self.max_sections is not None:
            if self.section_index < 0 or self.section_index >= self.max_sections:
                raise ValueError("section_index must be >= 0 and < max_sections")
        return self


# Removed: GenerateBlogRequest - Blog generation feature removed
# Removed: DocumentVersionSummary, DocumentVersionDetail, DocumentVersionList - Documents feature removed
# Removed: ExportTarget, DocumentExportRequest, DocumentExportResponse - Documents feature removed

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


# Removed: IngestTextRequest, IngestDriveRequest, DriveChangePollRequest - Documents feature removed

class Project(BaseModel):
    project_id: str
    document_id: str
    user_id: str
    youtube_url: Optional[str]
    title: Optional[str] = None
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
    # Removed: document field - Documents feature removed


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
    sections: Optional[Any] = None


class ProjectBlogDiff(BaseModel):
    project_id: str
    document_id: str
    from_version_id: str
    to_version_id: str
    changed_sections: List[str]
    diff_body_mdx: Optional[str] = None


class ProjectActivityResponse(BaseModel):
    """Activity feed for a single project combining jobs and pipeline events."""

    project_id: str
    items: List[Dict[str, Any]]


class SEOLevel(str, Enum):
    GOOD = "good"
    AVERAGE = "average"
    POOR = "poor"


class SEOScore(BaseModel):
    name: str
    label: str
    score: float = Field(ge=0.0, le=100.0)
    level: SEOLevel = Field(description="Simple qualitative bucket: good, average, poor")
    details: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SEOSuggestion(BaseModel):
    id: str
    title: str
    summary: str
    severity: Severity = Field(default=Severity.INFO, description="info, warning, or error")
    metric: Optional[str] = Field(default=None, description="Score that surfaced the suggestion")

    model_config = ConfigDict(use_enum_values=True)


class IssueLevel(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class SchemaIssue(BaseModel):
    code: str
    level: IssueLevel = Field(description="warning or error")
    message: str
    path: Optional[str] = None
    property: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class ValidationSeverity(str, Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


class ValidationSource(str, Enum):
    LOCAL = "local"
    SCHEMA_ORG = "schema.org"
    GOOGLE = "google"
    MIXED = "mixed"


class SchemaValidationResult(BaseModel):
    is_valid: bool
    severity: ValidationSeverity = Field(description="ok, warning, or error")
    issues: List[SchemaIssue] = Field(default_factory=list)
    schema_type: Optional[str] = None
    hint: Optional[str] = None
    source: Optional[ValidationSource] = Field(
        default=ValidationSource.LOCAL,
        description="local, schema.org, google, or mixed",
    )

    model_config = ConfigDict(use_enum_values=True)


class ProjectSEOAnalyzeRequest(BaseModel):
    target_keywords: Optional[List[str]] = Field(
        default=None,
        description="Optional keywords to prioritize during analysis.",
        max_length=20,
    )
    focus_keyword: Optional[str] = Field(
        default=None,
        description="Optional single focus keyword that should appear early in the article.",
        max_length=120,
    )
    content_type: Optional[str] = Field(
        default=None,
        description="Override content type hint for the analysis phase.",
        max_length=200,
    )
    schema_type: Optional[str] = Field(
        default=None,
        description="Override schema type for the analysis phase.",
        max_length=200,
    )


class ProjectSEOAnalysis(BaseModel):
    project_id: str
    document_id: str
    version_id: str
    content_type: Optional[str] = None
    content_type_hint: Optional[str] = None
    schema_type: Optional[str] = None
    seo: Dict[str, Any]
    scores: List[SEOScore]
    suggestions: List[SEOSuggestion]
    structured_content: Optional[Dict[str, Any]] = None
    word_count: int = Field(ge=0)
    reading_time_seconds: Optional[int] = Field(default=None, ge=0)
    generated_at: Optional[datetime] = None
    analyzed_at: Optional[datetime] = None
    is_cached: bool = Field(default=False)
    schema_validation: Optional[SchemaValidationResult] = None

    model_config = ConfigDict(use_enum_values=True)


class TranscriptProxyRequest(BaseModel):
    """Request model for YouTube transcript proxy endpoint."""
    
    video_id: str = Field(..., description="YouTube video ID (11 characters)", min_length=11, max_length=11)


class TranscriptProxyResponse(BaseModel):
    """Response model for YouTube transcript proxy endpoint."""
    
    success: bool
    transcript: Optional[Dict[str, Any]] = Field(default=None, description="Transcript data with text, format, language, track_kind")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Metadata including client_version, method, video_id")
    error: Optional[Dict[str, Any]] = Field(default=None, description="Error details when success is False")
    
    model_config = ConfigDict(use_enum_values=True)
