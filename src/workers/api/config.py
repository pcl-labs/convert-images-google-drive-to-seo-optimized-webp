"""Dataclass-based configuration loader."""

from __future__ import annotations

import os
import threading
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

    encryption_key: Optional[str] = None

    api_key_length: int = 32
    pbkdf2_iterations: int = 600_000

    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000

    d1_database: Optional[Any] = None
    queue: Optional[Any] = None
    dlq: Optional[Any] = None
    kv_namespace: Optional[Any] = None

    use_inline_queue: bool = True
    cloudflare_account_id: Optional[str] = None
    cloudflare_api_token: Optional[str] = None
    cf_queue_name: Optional[str] = None
    cf_queue_dlq: Optional[str] = None

    max_job_retries: int = 3
    job_timeout_seconds: int = 3600

    cors_origins: List[str] = field(default_factory=lambda: ["http://localhost:8000"])
    transcript_langs: List[str] = field(default_factory=lambda: ["en"])
    enable_drive_pipeline: bool = True
    static_files_dir: str = "./static"

    def __post_init__(self) -> None:
        self.environment = (self.environment or "development").lower()
        self.debug = _bool(self.debug)
        self.jwt_use_cookies = _bool(self.jwt_use_cookies)
        self.use_inline_queue = _bool(self.use_inline_queue)
        self.enable_drive_pipeline = _bool(self.enable_drive_pipeline)
        self.static_files_dir = str(self.static_files_dir or "./static")
        self.rate_limit_per_minute = _int(self.rate_limit_per_minute, 60)
        self.rate_limit_per_hour = _int(self.rate_limit_per_hour, 1000)
        self.api_key_length = _int(self.api_key_length, 32)
        self.pbkdf2_iterations = _int(self.pbkdf2_iterations, 600_000)
        self.max_job_retries = _int(self.max_job_retries, 3)
        self.job_timeout_seconds = _int(self.job_timeout_seconds, 3600)
        self.jwt_expiration_hours = _int(self.jwt_expiration_hours, 24)
        self.cors_origins = _list(self.cors_origins, default=["http://localhost:8000"])
        self.transcript_langs = _list(self.transcript_langs, default=["en"])
        if not self.jwt_secret_key:
            raise ValueError("JWT_SECRET_KEY is required")
        if self.environment == "production" and not self.encryption_key:
            raise ValueError("ENCRYPTION_KEY is required in production")
        if self.environment == "production" and self.use_inline_queue:
            raise ValueError("USE_INLINE_QUEUE=true is not allowed in production")
        if not self.use_inline_queue:
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
            dotenv_values = _load_dotenv(Path(".env"))
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
    with _settings_lock:
        for name in field_names:
            if hasattr(new_settings, name):
                setattr(settings, name, getattr(new_settings, name))
    return settings


_settings_lock = threading.Lock()
settings = Settings.from_env()
