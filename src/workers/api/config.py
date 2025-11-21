"""Dataclass-based configuration loader."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _list(value: Any, *, default: List[str], sep: str = ",") -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        items = [item.strip() for item in value.split(sep)]
        return [item for item in items if item]
    return list(default)


def _load_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, raw = stripped.split("=", 1)
            values[key.strip()] = raw.strip().strip('"').strip("'")
    except Exception:
        pass
    return values


@dataclass
class Settings:
    """
    Application settings loaded from environment variables.
    
    This class works in both local development and Cloudflare Workers:
    
    **Local Development:**
    - Reads from `.env` file (if present) and `os.environ`
    - `d1_database` is typically None, causing Database() to fall back to SQLite
    - `static_files_dir` can be set to a filesystem path for local dev
    - `queue` and `dlq` are None when `USE_INLINE_QUEUE=true` (default)
    
    **Cloudflare Workers:**
    - Reads from `os.environ` (populated by `wrangler.toml` vars + secrets)
    - `d1_database` is set from `env.DB` binding via `runtime.apply_worker_env()`
    - `static_files_dir` should be None (uses package-based loader)
    - `queue` and `dlq` are set from `env.JOB_QUEUE` and `env.DLQ` bindings
    
    See `src/workers/runtime.py` for how Worker bindings are injected.
    See `docs/CLOUDFLARE_WORKERS.md` for setup instructions.
    """
    app_name: str = "Quill API"
    app_version: str = "1.0.0"
    environment: str = "development"
    debug: bool = False
    base_url: Optional[str] = None

    github_client_id: Optional[str] = None
    github_client_secret: Optional[str] = None
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None

    jwt_secret_key: Optional[str] = None
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 24
    jwt_use_cookies: bool = True

    api_key_length: int = 32
    pbkdf2_iterations: int = 600_000

    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000

    session_cookie_name: str = "session_id"
    session_ttl_hours: int = 72
    session_touch_interval_seconds: int = 300

    d1_database: Optional[Any] = None
    queue: Optional[Any] = None
    dlq: Optional[Any] = None
    kv_namespace: Optional[Any] = None
    assets: Optional[Any] = None  # Cloudflare Assets binding for static files

    use_inline_queue: bool = False
    cloudflare_account_id: Optional[str] = None
    cloudflare_api_token: Optional[str] = None
    cf_queue_name: Optional[str] = None
    cf_queue_dlq: Optional[str] = None

    max_job_retries: int = 3
    job_timeout_seconds: int = 3600

    cors_origins: List[str] = field(default_factory=lambda: ["http://localhost:8000"])
    transcript_langs: List[str] = field(default_factory=lambda: ["en"])
    enable_drive_pipeline: bool = True
    enable_notifications: bool = False
    auto_generate_after_ingest: bool = True
    drive_webhook_url: Optional[str] = None
    drive_webhook_secret: Optional[str] = None
    drive_watch_renewal_window_minutes: int = 60
    static_files_dir: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_api_base: Optional[str] = None
    # Default OpenAI blog model (GPT-5.1 is the current target model).
    # See https://platform.openai.com/docs/models/gpt-5.1
    openai_blog_model: str = "gpt-5.1"
    openai_blog_temperature: float = 0.6
    openai_blog_max_output_tokens: int = 2200

    def __post_init__(self) -> None:
        self.environment = (self.environment or "development").lower()
        self.debug = _bool(self.debug)
        self.jwt_use_cookies = _bool(self.jwt_use_cookies)
        # Normalize any explicit USE_INLINE_QUEUE value first, then
        # override it based on environment so behavior is predictable:
        # - development => inline queue (no Cloudflare Queue required)
        # - production  => external queue (Cloudflare Queue required)
        self.use_inline_queue = _bool(self.use_inline_queue)
        if self.environment == "development":
            self.use_inline_queue = True
        elif self.environment == "production":
            self.use_inline_queue = False
        self.enable_notifications = _bool(self.enable_notifications)
        # static_files_dir: None means use package-based loader (Worker-compatible)
        # If set to a path, mount_static_files() will try filesystem first, then fall back to package
        if self.static_files_dir:
            self.static_files_dir = str(self.static_files_dir)
        else:
            self.static_files_dir = None
        self.rate_limit_per_minute = _int(self.rate_limit_per_minute, 60)
        self.rate_limit_per_hour = _int(self.rate_limit_per_hour, 1000)
        self.api_key_length = _int(self.api_key_length, 32)
        self.pbkdf2_iterations = _int(self.pbkdf2_iterations, 600_000)
        self.max_job_retries = _int(self.max_job_retries, 3)
        self.job_timeout_seconds = _int(self.job_timeout_seconds, 3600)
        self.jwt_expiration_hours = _int(self.jwt_expiration_hours, 24)
        self.session_ttl_hours = max(1, _int(self.session_ttl_hours, 72))
        self.session_touch_interval_seconds = max(30, _int(self.session_touch_interval_seconds, 300))
        self.cors_origins = _list(self.cors_origins, default=["http://localhost:8000"])
        self.transcript_langs = _list(self.transcript_langs, default=["en"])
        cookie_name = (self.session_cookie_name or "session_id").strip()
        self.session_cookie_name = cookie_name or "session_id"
        if self.session_touch_interval_seconds >= self.session_ttl_hours * 3600:
            raise ValueError(
                f"session_touch_interval_seconds ({self.session_touch_interval_seconds}) "
                f"must be less than session_ttl_hours ({self.session_ttl_hours} hours = "
                f"{self.session_ttl_hours * 3600} seconds)"
            )
        self.drive_watch_renewal_window_minutes = _int(self.drive_watch_renewal_window_minutes, 60)
        self.openai_blog_temperature = _float(self.openai_blog_temperature, 0.6)
        self.openai_blog_max_output_tokens = _int(self.openai_blog_max_output_tokens, 2200)
        if not self.jwt_secret_key:
            raise ValueError("JWT_SECRET_KEY is required")
        # In production we always require external queues; in development
        # we always run inline, so Cloudflare queue credentials are not
        # required there.
        if self.environment == "production" and self.use_inline_queue:
            raise ValueError("USE_INLINE_QUEUE=true is not allowed in production")
        if not self.use_inline_queue and self.environment == "production":
            if not self.cloudflare_account_id:
                raise ValueError("CLOUDFLARE_ACCOUNT_ID is required when USE_INLINE_QUEUE=false")
            if not self.cloudflare_api_token:
                raise ValueError("CLOUDFLARE_API_TOKEN is required when USE_INLINE_QUEUE=false")
            if not self.cf_queue_name:
                raise ValueError("CF_QUEUE_NAME is required when USE_INLINE_QUEUE=false")

    @classmethod
    def from_env(cls, **overrides: Any) -> "Settings":
        dotenv_values: Dict[str, str] = {}
        if os.getenv("PYTEST_DISABLE_DOTENV") != "1":
            # Try to find .env file relative to repo root
            # In wrangler dev, working directory might be different, so try multiple paths
            env_paths = [
                Path(".env"),  # Current directory
                Path(__file__).parent.parent.parent.parent / ".env",  # From config.py: src/workers/api/config.py -> repo root
            ]
            
            for env_path in env_paths:
                if env_path.exists():
                    dotenv_values = _load_dotenv(env_path)
                    if dotenv_values:
                        import logging
                        logger = logging.getLogger(__name__)
                        logger.debug(f"Loaded .env from {env_path} ({len(dotenv_values)} variables)")
                    break
        data: Dict[str, Any] = {}
        for field_info in fields(cls):
            name = field_info.name
            if name in overrides:
                data[name] = overrides[name]
                continue
            env_key = name.upper()
            if env_key in os.environ:
                data[name] = os.environ[env_key]
            elif env_key in dotenv_values:
                data[name] = dotenv_values[env_key]
        return cls(**data)


def replace_settings(new_settings: Settings) -> Settings:
    field_names = {info.name for info in fields(Settings)}
    # No lock needed - Workers are single-threaded per isolate
    for name in field_names:
        if hasattr(new_settings, name):
            setattr(settings, name, getattr(new_settings, name))
    return settings


# Note: No locks needed in Cloudflare Workers - each isolate is single-threaded
settings = Settings.from_env()
