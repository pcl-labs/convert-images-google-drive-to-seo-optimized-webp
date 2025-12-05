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


# Removed: test_optimize_endpoint_requires_auth - optimize endpoint removed


def test_jobs_endpoint_requires_auth(client):
    """Test that jobs endpoint requires authentication."""
    response = client.get("/api/v1/jobs")
    assert response.status_code == 401


def test_google_oauth_status_requires_auth(client):
    """Test that Google OAuth status endpoint requires authentication."""
    response = client.get("/auth/google/status")
    assert response.status_code == 401


def test_providers_status_requires_auth(client):
    """Test that providers status endpoint requires authentication."""
    response = client.get("/auth/providers/status")
    assert response.status_code == 401


# Removed: test_github_status_requires_auth - GitHub OAuth removed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
