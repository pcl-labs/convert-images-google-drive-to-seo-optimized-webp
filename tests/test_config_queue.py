import pytest

from src.workers.api.config import Settings
from src.workers.api.cloudflare_queue import QueueProducer


@pytest.fixture(autouse=True)
def clear_queue_env(monkeypatch):
    # Disable .env loading so init kwargs drive behavior
    monkeypatch.setenv("PYTEST_DISABLE_DOTENV", "1")
    monkeypatch.delenv("USE_INLINE_QUEUE", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
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
    # Test that inline queue is not allowed in production
    kwargs = base_kwargs(environment="production", use_inline_queue=True)
    with pytest.raises(ValueError) as exc:
        Settings(**kwargs)
    assert "not allowed in production" in str(exc.value)


def test_missing_cloudflare_account_id_when_not_inline():
    with pytest.raises(ValueError) as exc:
        Settings(**base_kwargs(
            environment="development",
            use_inline_queue=False,
            cloudflare_api_token="token",
            cf_queue_name="queue",
            # cloudflare_account_id missing
        ))
    assert "CLOUDFLARE_ACCOUNT_ID" in str(exc.value)


def test_missing_cloudflare_api_token_when_not_inline():
    with pytest.raises(ValueError) as exc:
        Settings(**base_kwargs(
            environment="development",
            use_inline_queue=False,
            cloudflare_account_id="acc",
            cf_queue_name="queue",
            # cloudflare_api_token missing
        ))
    assert "CLOUDFLARE_API_TOKEN" in str(exc.value)


def test_missing_cf_queue_name_when_not_inline():
    with pytest.raises(ValueError) as exc:
        Settings(**base_kwargs(
            environment="development",
            use_inline_queue=False,
            cloudflare_account_id="acc",
            cloudflare_api_token="token",
            # cf_queue_name missing
        ))
    assert "CF_QUEUE_NAME" in str(exc.value)


def test_valid_api_config_passes():
    s = Settings(**base_kwargs(
        environment="development",
        use_inline_queue=False,
        cloudflare_account_id="acc",
        cloudflare_api_token="token",
        cf_queue_name="queue",
        cf_queue_dlq="dlq",
    ))
    assert s.use_inline_queue is False


def test_queue_producer_skips_cloudflare_clients_without_account(monkeypatch):
    monkeypatch.setattr("src.workers.api.cloudflare_queue.settings.use_inline_queue", False)
    monkeypatch.setattr("src.workers.api.cloudflare_queue.settings.cloudflare_api_token", "token")
    monkeypatch.setattr("src.workers.api.cloudflare_queue.settings.cf_queue_name", "queue")
    monkeypatch.setattr("src.workers.api.cloudflare_queue.settings.cf_queue_dlq", "dlq")
    monkeypatch.setattr("src.workers.api.cloudflare_queue.settings.cloudflare_account_id", None)

    producer = QueueProducer(queue=None, dlq=None)
    # Access properties to trigger initialization
    assert producer.queue is None
    assert producer.dlq is None
