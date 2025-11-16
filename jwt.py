"""Minimal JWT stub providing encode/decode for offline tests."""
from __future__ import annotations

import json
from datetime import datetime


class ExpiredSignatureError(Exception):
    pass


class InvalidTokenError(Exception):
    pass


def _convert_datetime_to_timestamp(obj):
    """Recursively convert datetime objects to Unix timestamps."""
    if isinstance(obj, datetime):
        return int(obj.timestamp())
    elif isinstance(obj, dict):
        return {k: _convert_datetime_to_timestamp(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_datetime_to_timestamp(item) for item in obj]
    return obj


def encode(payload, key, algorithm="HS256"):
    try:
        # Convert datetime objects to Unix timestamps for JSON serialization
        converted_payload = _convert_datetime_to_timestamp(payload)
        return json.dumps(converted_payload)
    except Exception as exc:  # pragma: no cover
        raise InvalidTokenError(str(exc)) from exc


def decode(token, key, algorithms=None):
    try:
        return json.loads(token)
    except Exception as exc:
        raise InvalidTokenError(str(exc)) from exc
