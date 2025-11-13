"""
Configuration management for the application.
"""

import os
from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""
    
    # Application
    app_name: str = "Google Drive Image Optimizer API"
    app_version: str = "1.0.0"
    environment: str = os.getenv("ENVIRONMENT", "development")
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    
    # GitHub OAuth
    github_client_id: Optional[str] = os.getenv("GITHUB_CLIENT_ID")
    github_client_secret: Optional[str] = os.getenv("GITHUB_CLIENT_SECRET")
    github_redirect_uri: Optional[str] = os.getenv("GITHUB_REDIRECT_URI", "http://localhost:8000/auth/callback")
    
    # JWT
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-production")
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 24
    
    # API Keys
    api_key_length: int = 32
    
    # Rate Limiting
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    rate_limit_per_hour: int = int(os.getenv("RATE_LIMIT_PER_HOUR", "1000"))
    
    # Cloudflare Bindings (set by Cloudflare Workers runtime)
    d1_database: Optional[object] = None  # Will be bound at runtime
    queue: Optional[object] = None  # Will be bound at runtime
    kv_namespace: Optional[object] = None  # Optional KV for caching
    
    # Queue Configuration
    queue_name: str = os.getenv("QUEUE_NAME", "image-optimization-queue")
    dead_letter_queue_name: str = os.getenv("DEAD_LETTER_QUEUE_NAME", "image-optimization-dlq")
    
    # Job Configuration
    max_job_retries: int = 3
    job_timeout_seconds: int = 3600  # 1 hour
    
    # CORS
    cors_origins: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")
    
    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # Ignore extra fields from .env


# Global settings instance
settings = Settings()

