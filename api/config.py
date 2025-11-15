"""
Configuration management for the application.
"""

import base64
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
    app_name: str = "Quill API"
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
    
    # Encryption (Fernet)
    encryption_key: Optional[str] = None  # Required in production - base64 URL-safe 32-byte key (Fernet.generate_key())
    
    # API Keys
    api_key_length: int = 32
    pbkdf2_iterations: int = 600000  # OWASP recommendation for PBKDF2-HMAC-SHA256; can be tuned via env
    
    # Rate Limiting
    rate_limit_per_minute: int = Field(default=60)
    rate_limit_per_hour: int = Field(default=1000)
    
    # Cloudflare Bindings (set by Cloudflare Workers runtime)
    d1_database: Optional[object] = None  # Will be bound at runtime
    queue: Optional[object] = None  # Will be bound at runtime
    dlq: Optional[object] = None  # Dead letter queue binding (DLQ)
    kv_namespace: Optional[object] = None  # Optional KV for caching
    
    # Queue Configuration (Cloudflare)
    # - use_inline_queue=true: bypass Cloudflare Queues (worker polls DB)
    # - use_inline_queue=false: use Cloudflare Workers bindings if provided, otherwise HTTP API via cf_* fields
    use_inline_queue: bool = Field(default=True)  # Use in-memory queue for local dev
    cf_account_id: Optional[str] = None  # Cloudflare account ID (required when use_inline_queue=false)
    cf_api_token: Optional[str] = None  # Cloudflare API token for Queue HTTP API (required when use_inline_queue=false)
    cf_queue_name: Optional[str] = None  # Primary Cloudflare queue name used by the app
    cf_queue_dlq: Optional[str] = None  # Optional Cloudflare dead letter queue name
    
    # Job Configuration
    max_job_retries: int = 3
    job_timeout_seconds: int = 3600  # 1 hour
    
    # CORS - accept string or list, will be converted to list
    cors_origins: Union[str, list[str]] = Field(default="http://localhost:8000")

    # Transcript Configuration
    transcript_langs: Union[str, list[str]] = Field(default="en,en-US,en-GB")
    
    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, v: Optional[str]) -> Optional[str]:
        """Validate ENCRYPTION_KEY is a base64 URL-safe 32-byte key."""
        if v is None:
            return None
        try:
            raw = base64.urlsafe_b64decode(v)
        except Exception as e:
            raise ValueError("ENCRYPTION_KEY must be base64 URL-safe encoded") from e
        if len(raw) != 32:
            raise ValueError("ENCRYPTION_KEY must decode to exactly 32 bytes (use Fernet.generate_key())")
        return v

    @model_validator(mode="after")
    def require_encryption_key_in_production(self):
        if (self.environment or "").lower() == "production" and not self.encryption_key:
            raise ValueError("ENCRYPTION_KEY is required in production (provide a base64 URL-safe 32-byte key)")
        return self
    
    @model_validator(mode="after")
    def validate_queue_configuration(self):
        """Validate queue configuration based on environment."""
        is_production = (self.environment or "").lower() == "production"
        
        # In production, require real Cloudflare bindings (not inline queue)
        if is_production and self.use_inline_queue:
            raise ValueError(
                "USE_INLINE_QUEUE=true is not allowed in production. "
                "Production must use real Cloudflare Queue bindings. "
                "Set USE_INLINE_QUEUE=false and ensure queue bindings are configured in wrangler.toml"
            )
        
        # If using Cloudflare Queue API (not inline), require API credentials
        if not self.use_inline_queue:
            if not self.cf_account_id:
                raise ValueError(
                    "CF_ACCOUNT_ID is required when USE_INLINE_QUEUE=false. "
                    "Get your account ID with: wrangler whoami"
                )
            if not self.cf_api_token:
                raise ValueError(
                    "CF_API_TOKEN is required when USE_INLINE_QUEUE=false. "
                    "Create an API token in Cloudflare dashboard: https://dash.cloudflare.com/profile/api-tokens"
                )
            if not self.cf_queue_name:
                raise ValueError(
                    "CF_QUEUE_NAME is required when USE_INLINE_QUEUE=false. "
                    "Set to your Cloudflare queue name (e.g., 'quill-jobs')"
                )
        
        return self

    @model_validator(mode="after")
    def parse_transcript_langs(self):
        """Parse comma-separated transcript_langs string into a list."""
        if isinstance(self.transcript_langs, str):
            if "," in self.transcript_langs:
                self.transcript_langs = [lang.strip() for lang in self.transcript_langs.split(",") if lang.strip()]
            else:
                self.transcript_langs = [self.transcript_langs.strip()] if self.transcript_langs.strip() else ["en"]
        elif isinstance(self.transcript_langs, list):
            if not self.transcript_langs:
                self.transcript_langs = ["en"]
        else:
            self.transcript_langs = ["en"]
        return self

    @model_validator(mode="after")
    def parse_cors_origins(self):
        """Parse comma-separated CORS origins string into a list."""
        if isinstance(self.cors_origins, str):
            if "," in self.cors_origins:
                self.cors_origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
            else:
                self.cors_origins = [self.cors_origins.strip()] if self.cors_origins.strip() else ["http://localhost:8000"]
        elif isinstance(self.cors_origins, list):
            if not self.cors_origins:
                self.cors_origins = ["http://localhost:8000"]
        else:
            self.cors_origins = ["http://localhost:8000"]
        return self


# Global settings instance
settings = Settings()

