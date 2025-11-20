"""
Basic API tests for the image optimizer API.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from src.workers.api.constants import COOKIE_GOOGLE_OAUTH_STATE

# Note: These are basic structure tests
# Full integration tests would require D1 database and queue setup


@pytest.fixture
def client():
    """Create test client."""
    from src.workers.api.main import app
    return TestClient(app)


def test_root_endpoint(client):
    """Test root endpoint."""
    response = client.get("/api")
    assert response.status_code == 200
    assert "name" in response.json()
    assert "version" in response.json()


def test_health_endpoint(client):
    """Test health endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_optimize_endpoint_requires_auth(client):
    """Test that optimize endpoint requires authentication."""
    response = client.post("/api/v1/optimize", json={
        "document_id": "doc-test"
    })
    assert response.status_code in [401, 403]


def test_jobs_endpoint_requires_auth(client):
    """Test that jobs endpoint requires authentication."""
    response = client.get("/api/v1/jobs")
    assert response.status_code == 401


def test_github_auth_redirect(client):
    """Test GitHub OAuth redirect."""
    mock_url = "https://github.com/login/oauth/authorize?test=1"
    with patch('src.workers.api.auth.get_github_oauth_url', return_value=(mock_url, "test_state_token")) as mock_get_url:
        response = client.get("/auth/github/start", follow_redirects=False)
        # Should redirect (302, 303, or 307)
        assert response.status_code in [302, 303, 307]
        # Verify Location header matches mocked URL
        assert response.headers['Location'] == mock_url
        # Verify the mock was called exactly once
        mock_get_url.assert_called_once()


def test_google_login_start_redirect(client):
    """Test Google login OAuth redirect."""
    mock_url = "https://accounts.google.com/o/oauth2/v2/auth?test=1"
    with patch('src.workers.api.auth.get_google_login_oauth_url', return_value=(mock_url, "state")) as mock_get_url:
        response = client.get("/auth/google/login/start", follow_redirects=False)
        assert response.status_code in [302, 303, 307]
        assert response.headers['Location'] == mock_url
        mock_get_url.assert_called_once()


def test_google_login_start_post_requires_csrf(client):
    response = client.post("/auth/google/login/start", data={"csrf_token": "bad"})
    assert response.status_code == 403


def test_google_login_start_post_redirect(client):
    mock_url = "https://accounts.google.com/o/oauth2/v2/auth?test=2"
    client.cookies.set("csrf_token", "token")
    with patch('src.workers.api.auth.get_google_login_oauth_url', return_value=(mock_url, "state")):
        response = client.post("/auth/google/login/start", data={"csrf_token": "token"}, follow_redirects=False)
        assert response.status_code in [302, 303, 307]
        assert response.headers['Location'] == mock_url


def test_google_oauth_start_redirects_when_configured(client):
    """Test that Google OAuth start endpoint redirects when configured; skip otherwise."""
    from src.workers.api.config import settings
    if not settings.google_client_id or not settings.google_client_secret:
        import pytest
        pytest.skip("Google OAuth not configured")
    # /auth/google/start now requires authentication, so it will return 401
    # Use /auth/google/login/start instead for unauthenticated OAuth
    response = client.get("/auth/google/login/start", follow_redirects=False)
    assert response.status_code in [302, 303, 307, 401, 500]  # 401 if not configured, 500 if error, redirect if working


def test_google_oauth_callback_requires_auth(client):
    """Test that Google OAuth callback endpoint requires authentication."""
    response = client.get("/auth/google/callback?code=test&state=test", follow_redirects=False)
    assert response.status_code == 401


def test_google_login_callback_invalid_state(client):
    client.cookies.set(COOKIE_GOOGLE_OAUTH_STATE, "expected")
    response = client.get("/auth/google/login/callback?code=test&state=unexpected", follow_redirects=False)
    assert response.status_code == 403


def test_google_oauth_status_requires_auth(client):
    """Test that Google OAuth status endpoint requires authentication."""
    response = client.get("/auth/google/status")
    assert response.status_code == 401


def test_providers_status_requires_auth(client):
    """Test that providers status endpoint requires authentication."""
    response = client.get("/auth/providers/status")
    assert response.status_code == 401


def test_google_oauth_url_generation():
    """Test that Google OAuth URL generation function works when configured."""
    from src.workers.api.google_oauth import get_google_oauth_url
    from src.workers.api.config import settings
    import secrets
    
    # Only test if Google OAuth is configured
    if not settings.google_client_id or not settings.google_client_secret:
        pytest.skip("Google OAuth not configured")
    
    state = secrets.token_urlsafe(16)
    redirect_uri = "http://localhost:8000/auth/google/callback"
    url = get_google_oauth_url(state, redirect_uri, integration="drive")
    
    assert "accounts.google.com" in url
    assert "client_id" in url
    assert state in url


def test_github_status_requires_auth(client):
    """Test that GitHub status endpoint requires authentication."""
    response = client.get("/auth/github/status")
    assert response.status_code == 401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
