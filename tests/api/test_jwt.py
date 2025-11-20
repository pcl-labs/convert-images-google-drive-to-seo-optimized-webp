"""Tests for pure-Python JWT implementation."""
import pytest
import time
from datetime import datetime, timedelta, timezone

from src.workers.api.jwt import encode, decode, ExpiredSignatureError, InvalidTokenError


def test_encode_returns_string():
    """Test that encode returns a string token."""
    secret = "test-secret"
    payload = {"sub": "user-123", "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    
    assert isinstance(token, str)
    assert len(token) > 0
    # JWT should have 3 parts separated by dots
    assert len(token.split(".")) == 3


def test_decode_valid_token():
    """Test decoding a valid token."""
    secret = "test-secret"
    payload = {"sub": "user-123", "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    decoded = decode(token, secret, algorithms=["HS256"])
    
    assert decoded["sub"] == "user-123"
    assert decoded["exp"] == 9999999999


def test_decode_rejects_bad_signature():
    """Test that decode rejects tokens with tampered signatures."""
    secret = "test-secret"
    payload = {"sub": "user-123", "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    # Tamper with the signature (change last character)
    parts = token.split(".")
    tampered = f"{parts[0]}.{parts[1]}.{parts[2][:-1]}X"
    
    with pytest.raises(InvalidTokenError, match="Signature verification failed"):
        decode(tampered, secret, algorithms=["HS256"])


def test_decode_rejects_wrong_secret():
    """Test that decode rejects tokens signed with a different secret."""
    secret = "test-secret"
    wrong_secret = "wrong-secret"
    payload = {"sub": "user-123", "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    
    with pytest.raises(InvalidTokenError, match="Signature verification failed"):
        decode(token, wrong_secret, algorithms=["HS256"])


def test_decode_rejects_wrong_algorithm():
    """Test that decode rejects tokens with wrong algorithm."""
    secret = "test-secret"
    payload = {"sub": "user-123", "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    
    # Try to decode with algorithm not in allowed list
    with pytest.raises(InvalidTokenError, match="Algorithm not allowed"):
        decode(token, secret, algorithms=["RS256"])


def test_decode_rejects_unsupported_algorithm_in_header():
    """Test that decode rejects tokens with unsupported algorithm in header."""
    secret = "test-secret"
    payload = {"sub": "user-123", "exp": 9999999999}
    
    # Create a token manually with wrong algorithm in header
    import json
    import base64
    import hmac
    import hashlib
    
    header = {"alg": "RS256", "typ": "JWT"}
    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    token = f"{header_b64}.{payload_b64}.{sig_b64}"
    
    # The implementation correctly rejects algorithms not in the allowed list early
    with pytest.raises(InvalidTokenError, match="Algorithm not allowed"):
        decode(token, secret, algorithms=["HS256"])


def test_decode_enforces_expiration():
    """Test that decode raises ExpiredSignatureError for expired tokens."""
    secret = "test-secret"
    # Token expired 1 hour ago
    exp = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
    payload = {"sub": "user-123", "exp": exp}
    
    token = encode(payload, secret, algorithm="HS256")
    
    with pytest.raises(ExpiredSignatureError, match="Token has expired"):
        decode(token, secret, algorithms=["HS256"])


def test_decode_accepts_valid_expiration():
    """Test that decode accepts tokens with valid expiration."""
    secret = "test-secret"
    # Token expires in 1 hour
    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    payload = {"sub": "user-123", "exp": exp}
    
    token = encode(payload, secret, algorithm="HS256")
    decoded = decode(token, secret, algorithms=["HS256"])
    
    assert decoded["sub"] == "user-123"
    assert decoded["exp"] == exp


def test_decode_accepts_token_without_exp():
    """Test that decode accepts tokens without expiration claim."""
    secret = "test-secret"
    payload = {"sub": "user-123"}
    
    token = encode(payload, secret, algorithm="HS256")
    decoded = decode(token, secret, algorithms=["HS256"])
    
    assert decoded["sub"] == "user-123"
    assert "exp" not in decoded


def test_decode_rejects_invalid_jwt_format():
    """Test that decode rejects invalid JWT format."""
    secret = "test-secret"
    
    # Not enough parts
    with pytest.raises(InvalidTokenError, match="Not a JWT"):
        decode("invalid.token", secret, algorithms=["HS256"])
    
    # Too many parts
    with pytest.raises(InvalidTokenError, match="Not a JWT"):
        decode("part1.part2.part3.part4", secret, algorithms=["HS256"])


def test_encode_handles_datetime_objects():
    """Test that encode converts datetime objects to timestamps."""
    secret = "test-secret"
    exp_time = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"sub": "user-123", "exp": exp_time}
    
    token = encode(payload, secret, algorithm="HS256")
    decoded = decode(token, secret, algorithms=["HS256"])
    
    # exp should be converted to timestamp
    assert isinstance(decoded["exp"], (int, float))
    assert abs(decoded["exp"] - exp_time.timestamp()) < 1


def test_encode_handles_nested_datetime_objects():
    """Test that encode handles nested datetime objects."""
    secret = "test-secret"
    exp_time = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {
        "sub": "user-123",
        "exp": exp_time,
        "metadata": {
            "created_at": datetime.now(timezone.utc),
            "nested": {
                "updated_at": datetime.now(timezone.utc)
            }
        }
    }
    
    token = encode(payload, secret, algorithm="HS256")
    decoded = decode(token, secret, algorithms=["HS256"])
    
    assert isinstance(decoded["exp"], (int, float))
    assert isinstance(decoded["metadata"]["created_at"], (int, float))
    assert isinstance(decoded["metadata"]["nested"]["updated_at"], (int, float))


def test_decode_algorithm_case_insensitive():
    """Test that decode handles algorithm case insensitivity."""
    secret = "test-secret"
    payload = {"sub": "user-123", "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    
    # Should work with different cases
    decoded1 = decode(token, secret, algorithms=["hs256"])
    decoded2 = decode(token, secret, algorithms=["Hs256"])
    decoded3 = decode(token, secret, algorithms=["HS256"])
    
    assert decoded1["sub"] == "user-123"
    assert decoded2["sub"] == "user-123"
    assert decoded3["sub"] == "user-123"


def test_decode_with_none_algorithms_defaults_to_hs256():
    """Test that decode defaults to HS256 when algorithms is None."""
    secret = "test-secret"
    payload = {"sub": "user-123", "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    # Passing None should default to ["HS256"]
    decoded = decode(token, secret, algorithms=None)
    
    assert decoded["sub"] == "user-123"


def test_decode_defaults_to_hs256_when_algorithms_not_provided():
    """Test that decode defaults to HS256 when algorithms parameter is omitted."""
    secret = "test-secret"
    payload = {"sub": "user-123", "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    # Not passing algorithms parameter should default to ["HS256"]
    decoded = decode(token, secret)
    
    assert decoded["sub"] == "user-123"


def test_decode_handles_iat_claim():
    """Test that decode accepts and preserves iat (issued-at) claim."""
    secret = "test-secret"
    iat = int(datetime.now(timezone.utc).timestamp())
    payload = {"sub": "user-123", "iat": iat, "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    decoded = decode(token, secret, algorithms=["HS256"])
    
    assert decoded["iat"] == iat
    assert decoded["sub"] == "user-123"


def test_decode_handles_iat_as_datetime():
    """Test that decode handles iat claim when provided as datetime object."""
    secret = "test-secret"
    iat_time = datetime.now(timezone.utc)
    payload = {"sub": "user-123", "iat": iat_time, "exp": 9999999999}
    
    token = encode(payload, secret, algorithm="HS256")
    decoded = decode(token, secret, algorithms=["HS256"])
    
    # iat should be converted to timestamp
    assert isinstance(decoded["iat"], (int, float))
    assert abs(decoded["iat"] - iat_time.timestamp()) < 1


def test_decode_rejects_malformed_base64():
    """Test that decode rejects tokens with malformed base64 encoding."""
    secret = "test-secret"
    
    # Invalid base64 characters
    with pytest.raises(InvalidTokenError):
        decode("invalid.base64!@#.token", secret, algorithms=["HS256"])


def test_decode_rejects_malformed_json():
    """Test that decode rejects tokens with malformed JSON in header or payload."""
    secret = "test-secret"
    
    # Create token with invalid JSON in header
    import base64
    invalid_json = base64.urlsafe_b64encode(b"{invalid json}").rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(b'{"sub":"user-123"}').rstrip(b"=").decode()
    sig_b64 = base64.urlsafe_b64encode(b"fake-signature").rstrip(b"=").decode()
    token = f"{invalid_json}.{payload_b64}.{sig_b64}"
    
    with pytest.raises(InvalidTokenError):
        decode(token, secret, algorithms=["HS256"])


def test_decode_rejects_empty_token():
    """Test that decode rejects empty or whitespace-only tokens."""
    secret = "test-secret"
    
    with pytest.raises(InvalidTokenError, match="Not a JWT"):
        decode("", secret, algorithms=["HS256"])
    
    with pytest.raises(InvalidTokenError, match="Not a JWT"):
        decode("   ", secret, algorithms=["HS256"])


def test_decode_rejects_token_with_missing_alg_header():
    """Test that decode rejects tokens with missing alg in header."""
    secret = "test-secret"
    
    # Create token manually with missing alg
    import json
    import base64
    import hmac
    import hashlib
    
    header = {"typ": "JWT"}  # Missing "alg"
    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    payload = {"sub": "user-123", "exp": 9999999999}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    token = f"{header_b64}.{payload_b64}.{sig_b64}"
    
    # Should reject because alg is missing/empty
    with pytest.raises(InvalidTokenError, match="Algorithm not allowed"):
        decode(token, secret, algorithms=["HS256"])

