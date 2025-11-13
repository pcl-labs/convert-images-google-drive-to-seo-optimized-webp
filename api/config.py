"""
Configuration management for the application.
"""

from typing import Optional, Union
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore"  # Ignore extra fields from .env
    )
    
    # Application
    app_name: str = "Google Drive Image Optimizer API"
    app_version: str = "1.0.0"
    environment: str = Field(default="development")
    debug: bool = Field(default=False)
    base_url: Optional[str] = None  # Base URL for OAuth redirects (e.g., https://api.example.com). If not set, uses request URL.
    
    # GitHub OAuth - pydantic-settings automatically reads from env vars matching field names (case-insensitive)
    github_client_id: Optional[str] = None
    github_client_secret: Optional[str] = None
    # Note: Redirect URI is built from request URL automatically, no env var needed
    
    # Google OAuth (per-user linking)
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    # Note: Redirect URI is built from request URL automatically, no env var needed
    
    # JWT
    jwt_secret_key: str  # Required - must be set via JWT_SECRET_KEY env var
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 24
    jwt_use_cookies: bool = Field(default=True)
    
    # API Keys
    api_key_length: int = 32
    
    # Rate Limiting
    rate_limit_per_minute: int = Field(default=60)
    rate_limit_per_hour: int = Field(default=1000)
    
    # Cloudflare Bindings (set by Cloudflare Workers runtime)
    d1_database: Optional[object] = None  # Will be bound at runtime
    queue: Optional[object] = None  # Will be bound at runtime
    dlq: Optional[object] = None  # Dead letter queue binding (DLQ)
    kv_namespace: Optional[object] = None  # Optional KV for caching
    
    # Queue Configuration
    queue_name: str = Field(default="image-optimization-queue")
    dead_letter_queue_name: str = Field(default="image-optimization-dlq")
    
    # Job Configuration
    max_job_retries: int = 3
    job_timeout_seconds: int = 3600  # 1 hour
    
    # CORS - accept string or list, will be converted to list
    cors_origins: Union[str, list[str]] = Field(default="http://localhost:8000")
    
    @model_validator(mode="after")
    def parse_cors_origins(self):
        """Parse comma-separated CORS origins string into a list."""
        if isinstance(self.cors_origins, str):
            if "," in self.cors_origins:
                self.cors_origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
            else:
                self.cors_origins = [self.cors_origins.strip()] if self.cors_origins.strip() else ["http://localhost:8000"]
        elif not isinstance(self.cors_origins, list):
            self.cors_origins = ["http://localhost:8000"]
        return self


# Global settings instance
settings = Settings()

