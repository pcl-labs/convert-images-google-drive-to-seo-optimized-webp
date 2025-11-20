"""Encryption helpers built on top of ChaCha20-Poly1305."""

from __future__ import annotations

import base64
import binascii
import os
import threading
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from .config import settings

_VERSION = 1
_NONCE_SIZE = 12  # ChaCha20-Poly1305 nonce length

_KEY_BYTES: Optional[bytes] = None
_RAW_KEY: Optional[str] = None
_CIPHER: Optional[ChaCha20Poly1305] = None
_LOCK = threading.Lock()


def _get_cipher() -> ChaCha20Poly1305:
    global _KEY_BYTES, _RAW_KEY, _CIPHER
    raw_key = (settings.encryption_key or "").strip()
    if not raw_key:
        raise ValueError("ENCRYPTION_KEY is required")
    if _CIPHER is not None and _RAW_KEY == raw_key:
        return _CIPHER
    with _LOCK:
        raw_key = (settings.encryption_key or "").strip()
        if not raw_key:
            raise ValueError("ENCRYPTION_KEY is required")
        if _CIPHER is None or _RAW_KEY != raw_key:
            padded = raw_key + ("=" * ((4 - len(raw_key) % 4) % 4))
            key_bytes = base64.urlsafe_b64decode(padded)
            if len(key_bytes) != 32:
                raise ValueError("ENCRYPTION_KEY must decode to 32 bytes for ChaCha20-Poly1305")
            _KEY_BYTES = key_bytes
            _RAW_KEY = raw_key
            _CIPHER = ChaCha20Poly1305(_KEY_BYTES)
        return _CIPHER


def encrypt(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    cipher = _get_cipher()
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = cipher.encrypt(nonce, text.encode("utf-8"), None)
    payload = bytes([_VERSION]) + nonce + ciphertext
    return base64.urlsafe_b64encode(payload).decode("utf-8")


def decrypt(encrypted_text: Optional[str]) -> Optional[str]:
    if encrypted_text is None:
        return None
    cipher = _get_cipher()
    raw = encrypted_text.encode("utf-8")
    pad_len = (-len(raw)) % 4
    if pad_len:
        raw += b"=" * pad_len
    try:
        payload = base64.urlsafe_b64decode(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid encrypted payload") from exc
    if len(payload) < 1 + _NONCE_SIZE:
        raise ValueError("Encrypted payload too short")
    version = payload[0]
    if version != _VERSION:
        raise ValueError(f"Unsupported encryption version: {version}")
    nonce = payload[1 : 1 + _NONCE_SIZE]
    ciphertext = payload[1 + _NONCE_SIZE :]
    try:
        plaintext = cipher.decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:  # pragma: no cover - indicates tampering
        raise ValueError("Invalid encrypted payload") from exc
    return plaintext.decode("utf-8")


__all__ = ["encrypt", "decrypt"]
