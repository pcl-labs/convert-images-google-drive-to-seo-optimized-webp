"""Minimal subset of Pydantic interfaces for offline tests."""
from __future__ import annotations

from typing import Any, Callable


class BaseModel:
    model_config: dict[str, Any] = {}

    def __init__(self, **data: Any):
        for key, value in data.items():
            setattr(self, key, value)
        self.model_fields_set = set(data.keys())

    def model_dump(self) -> dict[str, Any]:  # pragma: no cover - helper only
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


def Field(default: Any = None, **_: Any):
    return default


def field_validator(*fields: str, **_: Any):
    def decorator(func: Callable[[Any, Any], Any]):
        return func

    return decorator


def model_validator(*, mode: str | None = None):
    def decorator(func: Callable[[Any], Any]):
        return func

    return decorator


ConfigDict = dict[str, Any]
HttpUrl = str
