"""
Pydantic models for request/response validation.
"""

from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime


class JobStatusEnum(str, Enum):
    """Job status enumeration."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OptimizeRequest(BaseModel):
    """Request model for image optimization."""
    
    drive_folder: str = Field(
        ...,
        description="Google Drive folder ID or share link",
        min_length=10,
        max_length=500
    )
    extensions: Optional[List[str]] = Field(
        default=["jpg", "jpeg", "png", "bmp", "tiff", "heic"],
        description="List of image extensions to process",
        max_items=10
    )
    overwrite: bool = Field(
        default=False,
        description="Overwrite existing optimized files"
    )
    skip_existing: bool = Field(
        default=True,
        description="Skip files that are already optimized"
    )
    cleanup_originals: bool = Field(
        default=True,
        description="Delete original images after optimization"
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Number of retry attempts for failed operations"
    )
    
    @validator('extensions')
    def validate_extensions(cls, v):
        """Validate image extensions."""
        allowed = {'jpg', 'jpeg', 'png', 'bmp', 'tiff', 'heic', 'webp'}
        if not v:
            return ["jpg", "jpeg", "png", "bmp", "tiff", "heic"]
        validated = []
        for ext in v:
            ext_clean = ext.lower().lstrip('.')
            if ext_clean in allowed:
                validated.append(ext_clean)
        if not validated:
            raise ValueError("At least one valid image extension required")
        return validated


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


class JobStatus(BaseModel):
    """Job status response model."""
    
    job_id: str
    user_id: str
    status: JobStatusEnum
    progress: JobProgress
    created_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    drive_folder: Optional[str] = None
    
    class Config:
        use_enum_values = True


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
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class StatsResponse(BaseModel):
    """API statistics response."""
    
    total_jobs: int
    completed_jobs: int
    failed_jobs: int
    pending_jobs: int
    processing_jobs: int
    total_users: int

