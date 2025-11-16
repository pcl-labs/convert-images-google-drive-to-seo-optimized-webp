"""Lightweight symmetric encryption helpers compatible with Pyodide."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import threading
from typing import Optional

from .config import settings

_KEY_BYTES: Optional[bytes] = None
_KEY_LOCK = threading.Lock()
_NONCE_SIZE = 16
_MAC_SIZE = 32  # sha256


def _decode_key() -> bytes:
    key_str = (settings.encryption_key or "").strip()
    if not key_str:
        raise ValueError("ENCRYPTION_KEY is required")
    pad_len = (-len(key_str)) % 4
    padded = key_str + ("=" * pad_len)
    try:
        decoded = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("ENCRYPTION_KEY must be base64 URL-safe encoded") from exc
    if len(decoded) != 32:
        raise ValueError("ENCRYPTION_KEY must decode to 32 bytes")
    return decoded


def _get_key() -> bytes:
    global _KEY_BYTES
    if _KEY_BYTES is not None:
        return _KEY_BYTES
    with _KEY_LOCK:
        if _KEY_BYTES is None:
            _KEY_BYTES = _decode_key()
    return _KEY_BYTES


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    stream = bytearray()
    counter = 0
    while len(stream) < length:
        counter_bytes = counter.to_bytes(4, "big")
        block = hmac.new(key, nonce + counter_bytes, hashlib.sha256).digest()
        stream.extend(block)
        counter += 1
    return bytes(stream[:length])


def encrypt(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    data = text.encode("utf-8")
    nonce = os.urandom(_NONCE_SIZE)
    key = _get_key()
    if data:
        stream = _keystream(key, nonce, len(data))
        cipher = bytes(a ^ b for a, b in zip(data, stream))
    else:
        cipher = b""
    mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    payload = nonce + mac + cipher
    return base64.urlsafe_b64encode(payload).decode("utf-8")


def decrypt(encrypted_text: Optional[str]) -> Optional[str]:
    if encrypted_text is None:
        return None
    raw = encrypted_text.encode("utf-8")
    pad_len = (-len(raw)) % 4
    if pad_len:
        raw += b"=" * pad_len
    try:
        payload = base64.urlsafe_b64decode(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid encrypted payload") from exc
    if len(payload) < _NONCE_SIZE + _MAC_SIZE:
        raise ValueError("Encrypted payload too short")
    nonce = payload[:_NONCE_SIZE]
    mac = payload[_NONCE_SIZE : _NONCE_SIZE + _MAC_SIZE]
    cipher = payload[_NONCE_SIZE + _MAC_SIZE :]
    key = _get_key()
    expected_mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise ValueError("Invalid authentication tag")
    if cipher:
        stream = _keystream(key, nonce, len(cipher))
        data = bytes(a ^ b for a, b in zip(cipher, stream))
    else:
        data = b""
    return data.decode("utf-8")


__all__ = ["encrypt", "decrypt"]
