import pytest

from api.config import Settings


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
    with pytest.raises(ValueError) as exc:
        Settings(**base_kwargs(environment="production", use_inline_queue=True))
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
