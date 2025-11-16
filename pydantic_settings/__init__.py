"""Minimal BaseSettings stub for offline tests."""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel


class BaseSettings(BaseModel):
    def __init__(self, **values: Any):
        annotations = {}
        for cls in reversed(self.__class__.mro()):
            annotations.update(getattr(cls, "__annotations__", {}))
        for name in annotations:
            if name in values:
                setattr(self, name, values[name])
                continue
            env_key = name.upper()
            if env_key in os.environ:
                setattr(self, name, os.environ[env_key])
                continue
            if hasattr(self.__class__, name):
                setattr(self, name, getattr(self.__class__, name))
            else:
                setattr(self, name, None)
        self.model_fields_set = set(values.keys())


class SettingsConfigDict(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
