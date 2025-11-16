"""Minimal JWT stub providing encode/decode for offline tests."""
from __future__ import annotations

import json


class ExpiredSignatureError(Exception):
    pass


class InvalidTokenError(Exception):
    pass


def encode(payload, key, algorithm="HS256"):
    try:
        return json.dumps(payload)
    except Exception as exc:  # pragma: no cover
        raise InvalidTokenError(str(exc)) from exc


def decode(token, key, algorithms=None):
    try:
        return json.loads(token)
    except Exception as exc:
        raise InvalidTokenError(str(exc)) from exc
