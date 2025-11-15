import os
import pytest

from api.config import Settings


@pytest.fixture(autouse=True)
def clear_queue_env(monkeypatch):
    # Disable .env loading so init kwargs drive behavior
    monkeypatch.setenv("PYTEST_DISABLE_DOTENV", "1")
    monkeypatch.delenv("USE_INLINE_QUEUE", raising=False)
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    monkeypatch.delenv("CF_QUEUE_NAME", raising=False)
    return


def base_kwargs(**overrides):
    # Minimum required settings
    vals = {
        "jwt_secret_key": "test-key",
    }
    vals.update(overrides)
    return vals


def test_inline_allowed_in_development():
    s = Settings(**base_kwargs(environment="development", use_inline_queue=True))
    assert s.use_inline_queue is True


def test_inline_disallowed_in_production():
    # Provide encryption_key to pass production validation, then test queue validation
    # Valid Fernet key (32 bytes base64-encoded)
    kwargs = base_kwargs(environment="production", use_inline_queue=True)
    kwargs["encryption_key"] = "VpfktYJB-hFBWpqy0JmD1Xz2h1m6D3-aMlPyOLEqLEA="  # Valid 32-byte Fernet key
    with pytest.raises(ValueError) as exc:
        Settings(**kwargs)
    assert "not allowed in production" in str(exc.value)


def test_missing_cf_account_id_when_not_inline():
    with pytest.raises(ValueError) as exc:
        Settings(**base_kwargs(
            environment="development",
            use_inline_queue=False,
            cf_api_token="token",
            cf_queue_name="queue",
            # cf_account_id missing
        ))
    assert "CF_ACCOUNT_ID" in str(exc.value)


def test_missing_cf_api_token_when_not_inline():
    with pytest.raises(ValueError) as exc:
        Settings(**base_kwargs(
            environment="development",
            use_inline_queue=False,
            cf_account_id="acc",
            cf_queue_name="queue",
            # cf_api_token missing
        ))
    assert "CF_API_TOKEN" in str(exc.value)


def test_missing_cf_queue_name_when_not_inline():
    with pytest.raises(ValueError) as exc:
        Settings(**base_kwargs(
            environment="development",
            use_inline_queue=False,
            cf_account_id="acc",
            cf_api_token="token",
            # cf_queue_name missing
        ))
    assert "CF_QUEUE_NAME" in str(exc.value)


def test_valid_api_config_passes():
    s = Settings(**base_kwargs(
        environment="development",
        use_inline_queue=False,
        cf_account_id="acc",
        cf_api_token="token",
        cf_queue_name="queue",
        cf_queue_dlq="dlq",
    ))
    assert s.use_inline_queue is False
