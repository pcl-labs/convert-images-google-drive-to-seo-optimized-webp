"""Lightweight symmetric encryption helpers compatible with Pyodide.

SECURITY WARNING: This is a custom cryptographic implementation designed for
Pyodide/WASM compatibility. Custom crypto is inherently risky and requires formal
security audit before production use.

Implementation details:
- Stream cipher using HMAC-SHA256 in counter mode (CTR-like)
- Encrypt-then-MAC authentication pattern
- 16-byte random nonces per encryption
- Separate keys for encryption and MAC via HMAC-based derivation

Known limitations:
- Counter is 32-bit, limiting keystream to ~137GB (2^32 * 32 bytes)
- No nonce reuse detection (relies on 16-byte random nonce collision resistance)
- Not formally verified for IND-CCA2 security

REQUIRED: Professional cryptographic security audit before production deployment.
"""

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
_KEY_ENCRYPTION: Optional[bytes] = None
_KEY_MAC: Optional[bytes] = None
_KEY_LOCK = threading.Lock()
_VERSION = 1
_NONCE_SIZE = 16
_MAC_SIZE = 32  # sha256
_COUNTER_SIZE = 4  # 32-bit counter limits keystream to ~137GB


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


def _derive_keys(master_key: bytes) -> tuple[bytes, bytes]:
    """Derive separate encryption and MAC keys from master key.
    
    Uses HMAC-SHA256 with distinct labels to ensure key separation.
    This is a simplified key derivation - HKDF would be more standard but
    HMAC-based derivation is acceptable for this use case.
    """
    k_enc = hmac.new(master_key, b"encryption", hashlib.sha256).digest()
    k_mac = hmac.new(master_key, b"authentication", hashlib.sha256).digest()
    return k_enc, k_mac


def _get_keys() -> tuple[bytes, bytes]:
    """Get derived encryption and MAC keys."""
    global _KEY_BYTES, _KEY_ENCRYPTION, _KEY_MAC
    if _KEY_ENCRYPTION is not None and _KEY_MAC is not None:
        return _KEY_ENCRYPTION, _KEY_MAC
    with _KEY_LOCK:
        if _KEY_ENCRYPTION is None or _KEY_MAC is None:
            if _KEY_BYTES is None:
                _KEY_BYTES = _decode_key()
            _KEY_ENCRYPTION, _KEY_MAC = _derive_keys(_KEY_BYTES)
    return _KEY_ENCRYPTION, _KEY_MAC


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate keystream using HMAC-SHA256 in counter mode.
    
    WARNING: Counter is 32-bit, limiting maximum keystream to 2^32 * 32 bytes (~137GB).
    This is acceptable for OAuth tokens but may be insufficient for large data.
    """
    stream = bytearray()
    counter = 0
    max_blocks = (1 << (8 * _COUNTER_SIZE))  # 2^32
    max_length = max_blocks * 32  # SHA-256 output size
    
    if length > max_length:
        raise ValueError(f"Requested keystream length {length} exceeds maximum {max_length} bytes")
    
    while len(stream) < length:
        if counter >= max_blocks:
            raise ValueError("Counter overflow: keystream length exceeds counter capacity")
        counter_bytes = counter.to_bytes(_COUNTER_SIZE, "big")
        block = hmac.new(key, nonce + counter_bytes, hashlib.sha256).digest()
        stream.extend(block)
        counter += 1
    return bytes(stream[:length])


def encrypt(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    data = text.encode("utf-8")
    nonce = os.urandom(_NONCE_SIZE)
    k_enc, k_mac = _get_keys()
    if data:
        stream = _keystream(k_enc, nonce, len(data))
        cipher = bytes(a ^ b for a, b in zip(data, stream))
    else:
        cipher = b""
    mac = hmac.new(k_mac, nonce + cipher, hashlib.sha256).digest()
    payload = bytes([_VERSION]) + nonce + mac + cipher
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
    if len(payload) < 1 + _NONCE_SIZE + _MAC_SIZE:
        raise ValueError("Encrypted payload too short")
    version = payload[0]
    if version != _VERSION:
        raise ValueError(f"Unsupported encryption version: {version}")
    nonce = payload[1 : 1 + _NONCE_SIZE]
    mac = payload[1 + _NONCE_SIZE : 1 + _NONCE_SIZE + _MAC_SIZE]
    cipher = payload[1 + _NONCE_SIZE + _MAC_SIZE :]
    k_enc, k_mac = _get_keys()
    expected_mac = hmac.new(k_mac, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise ValueError("Invalid authentication tag")
    if cipher:
        stream = _keystream(k_enc, nonce, len(cipher))
        data = bytes(a ^ b for a, b in zip(cipher, stream))
    else:
        data = b""
    return data.decode("utf-8")


__all__ = ["encrypt", "decrypt"]
