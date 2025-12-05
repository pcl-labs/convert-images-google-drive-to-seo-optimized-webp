"""Tests for YouTube transcript proxy endpoint."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import Request

from src.workers.api.proxy import (
    router,
    _identity_key,
    _is_identity_rate_limited,
    _error_response,
)
from src.workers.api.models import TranscriptProxyRequest, TranscriptProxyResponse
from src.workers.core.youtube_proxy import TranscriptProxyError


@pytest.fixture
def mock_user():
    """Mock Better Auth user identity."""
    return {
        "user_id": "user-123",
        "organization_id": "org-456",
        "session_id": "sess-789",
        "role": "user",
    }


@pytest.fixture
def mock_request():
    """Mock FastAPI Request."""
    request = MagicMock(spec=Request)
    request.headers = {}
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    return request


def test_identity_key_organization_priority(mock_user, mock_request):
    """Test that organization_id takes priority in identity key."""
    key = _identity_key(mock_user, mock_request)
    assert key == "org-456"


def test_identity_key_user_id_fallback(mock_user, mock_request):
    """Test that user_id is used when organization_id is missing."""
    user_no_org = {**mock_user}
    del user_no_org["organization_id"]
    key = _identity_key(user_no_org, mock_request)
    assert key == "user-123"


def test_identity_key_session_id_fallback(mock_request):
    """Test that session_id is used when org and user_id are missing."""
    user_minimal = {"session_id": "sess-789"}
    key = _identity_key(user_minimal, mock_request)
    assert key == "sess-789"


def test_identity_key_auth_header_fallback(mock_request):
    """Test that Authorization header is used as fallback."""
    user_empty = {}
    mock_request.headers = {"Authorization": "Bearer token123"}
    key = _identity_key(user_empty, mock_request)
    assert key == "Bearer token123"


def test_identity_key_ip_fallback(mock_request):
    """Test that client IP is used as fallback."""
    user_empty = {}
    mock_request.headers = {}
    key = _identity_key(user_empty, mock_request)
    assert key == "127.0.0.1"


def test_identity_key_anonymous_fallback(mock_request):
    """Test that anonymous is used when all else fails."""
    user_empty = {}
    mock_request.headers = {}
    mock_request.client.host = None
    key = _identity_key(user_empty, mock_request)
    assert key == "anonymous"


def test_is_identity_rate_limited_no_limits():
    """Test rate limiting when limits are disabled."""
    with patch("src.workers.api.proxy._rate_limits", return_value=(0, 0)):
        result = _is_identity_rate_limited("test-identity")
        assert result is False


def test_is_identity_rate_limited_below_limit():
    """Test rate limiting behavior up to and beyond the limit."""
    identity = "test-identity-rate-limit"
    
    # First call should return False (under limit)
    result = _is_identity_rate_limited(identity)
    assert result is False
    
    # Call repeatedly up to the minute limit (default is 60)
    # The limit check is >=, so 60 requests means we've hit the limit
    with patch("src.workers.api.proxy._rate_limits", return_value=(60, 1000)):
        # Make 59 calls (just under the limit), all should return False
        for i in range(59):
            result = _is_identity_rate_limited(identity)
            assert result is False, f"Call {i+1} should not be rate limited"
        
        # The 60th call should be rate limited (hits the limit)
        result = _is_identity_rate_limited(identity)
        assert result is True, "60th call should be rate limited (hits limit of 60)"


def test_error_response_structure():
    """Test that error responses have correct structure."""
    response = _error_response(
        "test_error",
        "Test error message",
        details={"extra": "info"},
        status_code=400,
    )
    assert response.status_code == 400
    content = response.body.decode() if hasattr(response.body, "decode") else str(response.body)
    assert "test_error" in content
    assert "Test error message" in content


# Note: Full endpoint tests require Better Auth service to be running
# These are tested via integration/e2e tests or manual testing
# Unit tests focus on the core logic (identity key extraction, rate limiting, error responses)


@pytest.mark.asyncio
async def test_pick_proxy_with_free_proxies_disabled():
    """Test that _pick_proxy returns None when free proxies disabled and no manual proxies."""
    from src.workers.core.youtube_proxy import _pick_proxy
    from src.workers.api.config import Settings
    
    with patch("src.workers.core.youtube_proxy.settings") as mock_settings:
        mock_settings.youtube_scraper_enable_free_proxies = False
        mock_settings.youtube_scraper_proxy_pool = []
        
        result = await _pick_proxy()
        assert result is None


@pytest.mark.asyncio
async def test_pick_proxy_with_manual_proxies():
    """Test that _pick_proxy returns from manual pool when free proxies disabled."""
    from src.workers.core.youtube_proxy import _pick_proxy
    from src.workers.api.config import Settings
    
    with patch("src.workers.core.youtube_proxy.settings") as mock_settings:
        mock_settings.youtube_scraper_enable_free_proxies = False
        mock_settings.youtube_scraper_proxy_pool = ["http://proxy1:8080", "http://proxy2:8080"]
        
        result = await _pick_proxy()
        assert result in ["http://proxy1:8080", "http://proxy2:8080"]


@pytest.mark.asyncio
async def test_pick_proxy_with_free_proxies_enabled():
    """Test that _pick_proxy uses proxy pool manager when free proxies enabled."""
    from src.workers.core.youtube_proxy import _pick_proxy
    from unittest.mock import MagicMock, AsyncMock
    import asyncio
    
    with patch("src.workers.core.youtube_proxy.settings") as mock_settings, \
         patch("src.workers.core.youtube_proxy.get_proxy_pool_manager") as mock_get_manager:
        
        mock_settings.youtube_scraper_enable_free_proxies = True
        mock_manager = MagicMock()
        mock_manager.get_next_proxy.return_value = "http://free-proxy:8080"
        mock_manager.refresh_pool = AsyncMock()
        mock_get_manager.return_value = mock_manager
        
        result = await _pick_proxy()
        assert result == "http://free-proxy:8080"
        # Give the task a moment to be created
        await asyncio.sleep(0.01)
        # refresh_pool is called via create_task, so we can't easily assert it was called
        # but we can verify get_next_proxy was called
        mock_manager.get_next_proxy.assert_called_once()
