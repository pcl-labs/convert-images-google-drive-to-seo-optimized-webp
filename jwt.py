"""Minimal JWT stub providing encode/decode for offline tests."""
from __future__ import annotations

import os
import sys
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict

# Best-effort: load .env so ENVIRONMENT/DEBUG are available during import in dev
try:  # pragma: no cover - optional convenience
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


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


def _b64url_encode(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    import base64
    pad = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def encode(payload: Dict[str, Any], key: str, algorithm: str = "HS256") -> str:
    try:
        header = {"alg": algorithm, "typ": "JWT"}
        # Convert datetime objects to Unix timestamps for JSON serialization
        converted_payload = _convert_datetime_to_timestamp(payload)
        header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        payload_b64 = _b64url_encode(json.dumps(converted_payload, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        if algorithm.upper() == "HS256":
            sig = hmac.new(key.encode("utf-8"), signing_input, hashlib.sha256).digest()
        else:  # pragma: no cover - only HS256 supported in tests
            raise InvalidTokenError(f"Unsupported algorithm: {algorithm}")
        signature_b64 = _b64url_encode(sig)
        return f"{header_b64}.{payload_b64}.{signature_b64}"
    except Exception as exc:  # pragma: no cover
        raise InvalidTokenError(str(exc)) from exc


def decode(token: str, key: str, algorithms=None) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise InvalidTokenError("Not a JWT")
        header_b64, payload_b64, sig_b64 = parts
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        algo = (header.get("alg") or "").upper()
        if algorithms is not None and algo not in [a.upper() for a in algorithms]:
            raise InvalidTokenError("Algorithm not allowed")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        if algo == "HS256":
            expected = hmac.new(key.encode("utf-8"), signing_input, hashlib.sha256).digest()
        else:  # pragma: no cover - only HS256 supported in tests
            raise InvalidTokenError(f"Unsupported algorithm: {algo}")
        actual = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected, actual):
            raise InvalidTokenError("Signature verification failed")
        # Expiration check (exp is seconds since epoch)
        exp = payload.get("exp")
        if exp is not None:
            try:
                if isinstance(exp, (int, float)):
                    exp_ts = float(exp)
                elif isinstance(exp, str):
                    # Attempt RFC3339 parse, fallback to float
                    try:
                        exp_ts = datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        exp_ts = float(exp)
                elif isinstance(exp, datetime):
                    exp_ts = exp.timestamp()
                else:
                    exp_ts = float(exp)
            except Exception:
                raise InvalidTokenError("Invalid exp claim")
            now_ts = time.time()
            if now_ts >= exp_ts:
                raise ExpiredSignatureError("Token has expired")
        return payload
    except ExpiredSignatureError:
        raise
    except Exception as exc:
        raise InvalidTokenError(str(exc)) from exc


# Fail loudly in production; allow under pytest or development convenience
_is_pytest = ("pytest" in sys.modules) or (os.getenv("PYTEST_CURRENT_TEST") is not None)
_env = (os.getenv("ENVIRONMENT") or "").lower().strip()
_debug = (os.getenv("DEBUG") or "").lower().strip() in {"1", "true", "yes", "on"}
_is_dev = _env in {"development", "dev", "local"} or _debug
if not (_is_pytest or _is_dev):  # pragma: no cover - sanity guard
    raise RuntimeError(
        "jwt stub is for testing/development only and must not be used in production. Install PyJWT for real JWT support."
    )
