import base64
import os

import pytest

from src.workers.api import crypto
from src.workers.api.config import settings


@pytest.fixture(autouse=True)
def temp_encryption_key():
    original_key = settings.encryption_key
    new_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    settings.encryption_key = new_key
    crypto._RAW_KEY = None  # type: ignore[attr-defined]
    crypto._CIPHER = None  # type: ignore[attr-defined]
    yield
    settings.encryption_key = original_key
    crypto._RAW_KEY = None  # type: ignore[attr-defined]
    crypto._CIPHER = None  # type: ignore[attr-defined]


def test_encrypt_decrypt_round_trip():
    plaintext = "sensitive-token"
    encrypted = crypto.encrypt(plaintext)
    assert isinstance(encrypted, str)
    decrypted = crypto.decrypt(encrypted)
    assert decrypted == plaintext


def test_decrypt_rejects_tampering():
    token = crypto.encrypt("payload")
    tampered = token[:-2] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(ValueError):
        crypto.decrypt(tampered)
