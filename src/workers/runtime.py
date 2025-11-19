"""
Helpers for adapting Cloudflare Worker bindings into our Settings object.

NOTE: This module mutates process-wide environment variables so downstream
code can rely on `os.environ`. When spawning subprocesses, build a sanitized
environment explicitly rather than relying on these globals.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict

# Import Settings/replace_settings inside apply_worker_env() to avoid
# importing config.py before Worker env is applied (which would cause
# config.py to evaluate Settings.from_env() without JWT_SECRET_KEY)


WORKER_DB_BINDING = "DB"
WORKER_QUEUE_BINDING = "JOB_QUEUE"
WORKER_DLQ_BINDING = "DLQ"
WORKER_KV_BINDING = "KV"

_ENV_LOCK = threading.Lock()
_KEY_PATTERN = re.compile(r"^[A-Z0-9_]+$")
_MAX_VALUE_LENGTH = 4096


def _string_bindings_from_env(env: Any) -> Dict[str, str]:
    """
    Extract string environment variables from the Worker env object so
    BaseSettings can pick them up on instantiation.
    
    Worker secrets and vars can be accessed as:
    - Direct attributes: env.JWT_SECRET_KEY
    - Via __dict__: env.__dict__['JWT_SECRET_KEY']
    """
    string_values: Dict[str, str] = {}

    if env is None:
        return string_values

    # Try to get all attributes from env
    # Cloudflare Workers Python exposes secrets/vars as direct attributes
    try:
        # Use getattr with hasattr to safely check for attributes
        # Check common secret/env var names explicitly
        known_vars = [
            "JWT_SECRET_KEY", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET",
            "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ENVIRONMENT", "DEBUG",
            "BASE_URL", "CORS_ORIGINS", "RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_PER_HOUR",
            "JWT_USE_COOKIES", "JWT_ALGORITHM", "JWT_EXPIRATION_HOURS",
            "USE_INLINE_QUEUE", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
            "CF_QUEUE_NAME", "CF_QUEUE_DLQ", "TRANSCRIPT_LANGS", "OPENAI_API_KEY"
        ]
        
        for var_name in known_vars:
            if hasattr(env, var_name):
                try:
                    value = getattr(env, var_name)
                    if isinstance(value, str):
                        string_values[var_name] = value
                except (AttributeError, TypeError):
                    continue
    except Exception:
        pass

    # Also check __dict__ for any additional values
    try:
        env_dict = getattr(env, "__dict__", {})
        if isinstance(env_dict, dict):
            for attr, value in env_dict.items():
                if not attr.startswith("_") and isinstance(value, str):
                    # Don't overwrite if already found via direct access
                    if attr not in string_values:
                        string_values[attr] = value
    except Exception:
        pass

    return string_values


def apply_worker_env(env: Any) -> Settings:
    """
    Convert Cloudflare Worker env bindings into our Settings object and
    update the shared module-level settings in-place.
    """
    string_env = _string_bindings_from_env(env)
    allowed = {
        "ENVIRONMENT",
        "DEBUG",
        "BASE_URL",
        "CORS_ORIGINS",
        "RATE_LIMIT_PER_MINUTE",
        "RATE_LIMIT_PER_HOUR",
        "JWT_USE_COOKIES",
        "JWT_SECRET_KEY",
        "JWT_ALGORITHM",
        "JWT_EXPIRATION_HOURS",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "USE_INLINE_QUEUE",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "CF_QUEUE_NAME",
        "CF_QUEUE_DLQ",
        "TRANSCRIPT_LANGS",
    }
    protected = {"PATH", "HOME", "USER", "SHELL", "LD_LIBRARY_PATH", "PYTHONPATH"}
    sanitized: Dict[str, str] = {}
    for key, value in string_env.items():
        if key in allowed and key not in protected:
            if not _KEY_PATTERN.match(key):
                continue
            if not isinstance(value, str):
                continue
            if len(value) > _MAX_VALUE_LENGTH or any(ord(ch) < 32 for ch in value):
                continue
            sanitized[key] = value

    with _ENV_LOCK:
        for key, value in sanitized.items():
            os.environ[key] = value

    worker_kwargs = {}
    if env is not None:
        if hasattr(env, WORKER_DB_BINDING):
            worker_kwargs["d1_database"] = getattr(env, WORKER_DB_BINDING)
        if hasattr(env, WORKER_QUEUE_BINDING):
            worker_kwargs["queue"] = getattr(env, WORKER_QUEUE_BINDING)
        if hasattr(env, WORKER_DLQ_BINDING):
            worker_kwargs["dlq"] = getattr(env, WORKER_DLQ_BINDING)
        if hasattr(env, WORKER_KV_BINDING):
            worker_kwargs["kv_namespace"] = getattr(env, WORKER_KV_BINDING)

    # Import here (after os.environ is set) to avoid config.py evaluating
    # Settings.from_env() before Worker secrets are available
    from api.config import Settings, replace_settings
    
    # Instantiate a new Settings to evaluate BaseSettings sources with the
    # freshly injected os.environ values, then mutate the global instance.
    new_settings = Settings.from_env(**worker_kwargs)
    replace_settings(new_settings)
    return new_settings


__all__ = ["apply_worker_env"]
