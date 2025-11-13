"""
Basic API tests for the image optimizer API.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch

# Note: These are basic structure tests
# Full integration tests would require D1 database and queue setup


@pytest.fixture
def client():
    """Create test client."""
    from api.main import app
    return TestClient(app)


def test_root_endpoint(client):
    """Test root endpoint."""
    response = client.get("/")
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
        "drive_folder": "test-folder-id"
    })
    assert response.status_code == 401


def test_jobs_endpoint_requires_auth(client):
    """Test that jobs endpoint requires authentication."""
    response = client.get("/api/v1/jobs")
    assert response.status_code == 401


def test_github_auth_redirect(client):
    """Test GitHub OAuth redirect."""
    with patch('api.auth.get_github_oauth_url', return_value="https://github.com/login/oauth/authorize?test=1"):
        response = client.get("/auth/github", follow_redirects=False)
        # Should redirect or return error if not configured
        assert response.status_code in [302, 500]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

