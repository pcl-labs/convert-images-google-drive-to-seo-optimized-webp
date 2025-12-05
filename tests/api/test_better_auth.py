"""Tests for Better Auth integration."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Request, HTTPException, status

from src.workers.api.better_auth import (
    authenticate_with_better_auth,
    _session_headers,
    _extract_identity,
)
from src.workers.api.simple_http import SimpleResponse


def test_session_headers_with_auth():
    """Test extracting session headers with Authorization."""
    request = MagicMock(spec=Request)
    request.headers = {
        "Authorization": "Bearer token123",
        "Cookie": "session=abc",
    }
    
    headers = _session_headers(request)
    assert headers["Authorization"] == "Bearer token123"
    assert headers["Cookie"] == "session=abc"


def test_session_headers_no_auth():
    """Test extracting session headers without Authorization."""
    request = MagicMock(spec=Request)
    request.headers = {
        "Cookie": "session=abc",
    }
    
    headers = _session_headers(request)
    assert "Authorization" not in headers
    assert headers["Cookie"] == "session=abc"


def test_extract_identity_from_user():
    """Test extracting identity when user object has id."""
    result = {
        "user": {"id": "user-123", "role": "admin"},
        "session": {"id": "sess-456"},
    }
    
    identity = _extract_identity(result)
    assert identity["user_id"] == "user-123"
    assert identity["session_id"] == "sess-456"
    assert identity["role"] == "admin"


def test_extract_identity_from_session():
    """Test extracting identity when session has userId."""
    result = {
        "session": {"id": "sess-456", "userId": "user-789"},
    }
    
    identity = _extract_identity(result)
    assert identity["user_id"] == "user-789"
    assert identity["session_id"] == "sess-456"


def test_extract_identity_missing_user_id():
    """Test that missing user_id raises HTTPException."""
    result = {
        "session": {"id": "sess-456"},
    }
    
    with pytest.raises(HTTPException) as exc_info:
        _extract_identity(result)
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_extract_identity_with_organization():
    """Test extracting identity with organization_id."""
    result = {
        "user": {"id": "user-123", "organizationId": "org-456"},
        "session": {"id": "sess-789"},
    }
    
    identity = _extract_identity(result)
    assert identity["user_id"] == "user-123"
    assert identity["organization_id"] == "org-456"
    assert identity["session_id"] == "sess-789"


@pytest.mark.asyncio
async def test_authenticate_with_better_auth_success():
    """Test successful Better Auth authentication."""
    request = MagicMock(spec=Request)
    request.headers = {"Authorization": "Bearer token123"}
    
    mock_response = MagicMock(spec=SimpleResponse)
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "user": {"id": "user-123", "role": "user"},
        "session": {"id": "sess-456"},
    }
    
    with patch("src.workers.api.better_auth.settings") as mock_settings, \
         patch("src.workers.api.better_auth.AsyncSimpleClient") as mock_client:
        
        mock_settings.better_auth_base_url = "https://getquillio.com"
        mock_settings.better_auth_session_endpoint = "/api/auth/get-session"
        mock_settings.better_auth_timeout_seconds = 10.0
        
        mock_client_instance = AsyncMock()
        mock_client_instance.__aenter__.return_value = mock_client_instance
        mock_client_instance.__aexit__.return_value = None
        mock_client_instance.get.return_value = mock_response
        mock_client.return_value = mock_client_instance
        
        identity = await authenticate_with_better_auth(request)
        
        assert identity["user_id"] == "user-123"
        assert identity["session_id"] == "sess-456"
        assert identity["role"] == "user"


@pytest.mark.asyncio
async def test_authenticate_with_better_auth_missing_config():
    """Test that missing BETTER_AUTH_BASE_URL raises error."""
    request = MagicMock(spec=Request)
    request.headers = {"Authorization": "Bearer token123"}
    
    with patch("src.workers.api.better_auth.settings") as mock_settings:
        mock_settings.better_auth_base_url = None
        
        with pytest.raises(HTTPException) as exc_info:
            await authenticate_with_better_auth(request)
        assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


@pytest.mark.asyncio
async def test_authenticate_with_better_auth_no_credentials():
    """Test that missing credentials raises 401."""
    request = MagicMock(spec=Request)
    request.headers = {}
    
    with patch("src.workers.api.better_auth.settings") as mock_settings:
        mock_settings.better_auth_base_url = "https://getquillio.com"
        
        with pytest.raises(HTTPException) as exc_info:
            await authenticate_with_better_auth(request)
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_authenticate_with_better_auth_401_response():
    """Test that 401 from Better Auth raises HTTPException."""
    request = MagicMock(spec=Request)
    request.headers = {"Authorization": "Bearer invalid-token"}
    
    mock_response = MagicMock(spec=SimpleResponse)
    mock_response.status_code = 401
    
    with patch("src.workers.api.better_auth.settings") as mock_settings, \
         patch("src.workers.api.better_auth.AsyncSimpleClient") as mock_client:
        
        mock_settings.better_auth_base_url = "https://getquillio.com"
        mock_settings.better_auth_session_endpoint = "/api/auth/get-session"
        mock_settings.better_auth_timeout_seconds = 10.0
        
        mock_client_instance = AsyncMock()
        mock_client_instance.__aenter__.return_value = mock_client_instance
        mock_client_instance.__aexit__.return_value = None
        mock_client_instance.get.return_value = mock_response
        mock_client.return_value = mock_client_instance
        
        with pytest.raises(HTTPException) as exc_info:
            await authenticate_with_better_auth(request)
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
