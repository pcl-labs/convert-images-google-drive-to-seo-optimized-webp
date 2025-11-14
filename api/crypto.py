"""
Encryption utilities for sensitive data at rest.

Uses Fernet (symmetric encryption) with a key derived from JWT secret key.
"""

import hashlib
import base64
from cryptography.fernet import Fernet
from .config import settings

# Cache the Fernet instance to avoid recreating it on every call
_fernet_instance: Fernet | None = None


def _get_fernet_key() -> bytes:
    """
    Derive a Fernet-compatible key (32 bytes) from the JWT secret key.
    
    Fernet requires a 32-byte key, so we use SHA256 to hash the JWT secret
    and then base64-encode it in the format Fernet expects.
    """
    # Hash the JWT secret key to get exactly 32 bytes
    key_hash = hashlib.sha256(settings.jwt_secret_key.encode('utf-8')).digest()
    # Fernet expects a URL-safe base64-encoded 32-byte key
    return base64.urlsafe_b64encode(key_hash)


def _get_fernet() -> Fernet:
    """Get or create the Fernet instance (cached)."""
    global _fernet_instance
    if _fernet_instance is None:
        key = _get_fernet_key()
        _fernet_instance = Fernet(key)
    return _fernet_instance


def encrypt(text: str) -> str:
    """
    Encrypt a plaintext string.
    
    Args:
        text: Plaintext string to encrypt
        
    Returns:
        Encrypted string (base64-encoded)
        
    Raises:
        Exception: If encryption fails
    """
    if not text:
        return text
    fernet = _get_fernet()
    encrypted_bytes = fernet.encrypt(text.encode('utf-8'))
    return encrypted_bytes.decode('utf-8')


def decrypt(encrypted_text: str) -> str:
    """
    Decrypt an encrypted string.
    
    Args:
        encrypted_text: Encrypted string (base64-encoded)
        
    Returns:
        Decrypted plaintext string
        
    Raises:
        Exception: If decryption fails (e.g., invalid token, tampered data)
    """
    if not encrypted_text:
        return encrypted_text
    fernet = _get_fernet()
    decrypted_bytes = fernet.decrypt(encrypted_text.encode('utf-8'))
    return decrypted_bytes.decode('utf-8')

