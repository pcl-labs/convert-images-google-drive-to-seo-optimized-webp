import base64
import os

import pytest

from src.workers.api import crypto
from src.workers.api.config import settings


@pytest.fixture
def temp_encryption_key(monkeypatch):
    new_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(settings, "encryption_key", new_key)
    monkeypatch.setattr(crypto, "_RAW_KEY", None, raising=False)
    monkeypatch.setattr(crypto, "_CIPHER", None, raising=False)


def test_encrypt_decrypt_round_trip(temp_encryption_key):
    plaintext = "sensitive-token"
    encrypted = crypto.encrypt(plaintext)
    assert isinstance(encrypted, str)
    decrypted = crypto.decrypt(encrypted)
    assert decrypted == plaintext


def test_decrypt_rejects_tampering(temp_encryption_key):
    token = crypto.encrypt("payload")
    tampered = token[:-2] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(ValueError):
        crypto.decrypt(tampered)
