"""
Integration tests for the API against a running server.
These tests require the server to be running: python run_api.py
Tests endpoints without requiring Cloudflare bindings.
"""

import pytest
import requests
import json
import time

BASE_URL = "http://localhost:8000"


def _check_server_available():
    """Check if server is available, skip test if not."""
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=2)
        return True
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        pytest.skip("Server not available. Start with: python run_api.py")


@pytest.fixture(autouse=True)
def check_server():
    """Auto-check server availability before each test."""
    _check_server_available()


def test_health():
    """Test health endpoint."""
    response = requests.get(f"{BASE_URL}/health")
    assert response.status_code == 200
    print(f"Response status code: {response.status_code}")
    try:
        data = response.json()
        response_data = json.dumps(data, indent=2)
    except (json.JSONDecodeError, ValueError):
        data = None
        response_data = response.text
        print(f"Failed to parse JSON response. Response text: {response_data}")
    print(f"Response data: {response_data}")
    assert data is not None, f"Expected JSON response, got: {response_data}"
    assert data["status"] == "healthy"
    assert "version" in data


def test_root():
    """Test root endpoint."""
    response = requests.get(f"{BASE_URL}/")
    assert response.status_code == 200
    print(f"Response status code: {response.status_code}")
    try:
        data = response.json()
        response_data = json.dumps(data, indent=2)
    except (json.JSONDecodeError, ValueError):
        data = None
        response_data = response.text
        print(f"Failed to parse JSON response. Response text: {response_data}")
    print(f"Response data: {response_data}")
    assert data is not None, f"Expected JSON response, got: {response_data}"
    assert "name" in data
    assert "version" in data
    assert "endpoints" in data


def test_docs():
    """Test docs endpoint."""
    response = requests.get(f"{BASE_URL}/docs")
    assert response.status_code == 200


def test_optimize_requires_auth():
    """Test that optimize endpoint requires authentication."""
    response = requests.post(
        f"{BASE_URL}/api/v1/optimize",
        json={
            "drive_folder": "test-folder-id"
        }
    )
    assert response.status_code == 401
    try:
        data = response.json()
        response_data = json.dumps(data, indent=2)
    except (json.JSONDecodeError, ValueError):
        data = None
        response_data = response.text
    print(f"Response data: {response_data}")
    assert data is not None, f"Expected JSON response, got: {response_data}"
    assert "error" in data or "detail" in data


def test_jobs_requires_auth():
    """Test that jobs endpoint requires authentication."""
    response = requests.get(f"{BASE_URL}/api/v1/jobs")
    assert response.status_code == 401
    try:
        data = response.json()
        response_data = json.dumps(data, indent=2)
    except (json.JSONDecodeError, ValueError):
        data = None
        response_data = response.text
    print(f"Response data: {response_data}")
    assert data is not None, f"Expected JSON response, got: {response_data}"
    assert "error" in data or "detail" in data


def test_github_auth():
    """Test GitHub auth endpoint."""
    response = requests.get(f"{BASE_URL}/auth/github/start", allow_redirects=False)
    print(f"Response status code: {response.status_code}")
    print(f"Response headers: {response.headers}")
    print(f"Response text: {response.text[:200] if response.text else 'No response body'}")
    
    # Accept redirect status codes (302, 307) when OAuth is configured, or 500 error when not configured
    assert response.status_code in [302, 307, 500], f"Expected 302, 307, or 500, got {response.status_code}"
    
    if response.status_code in [302, 307]:
        # OAuth is configured - should redirect to GitHub
        assert "location" in response.headers
        assert "github.com" in response.headers["location"].lower()
        return
    
    if response.status_code == 500:
        # When OAuth is not configured, should return error message
        try:
            data = response.json()
            assert "detail" in data or "error" in data
        except (json.JSONDecodeError, ValueError):
            pass  # Non-JSON error response is also acceptable


def test_github_status_requires_auth():
    """Test that GitHub status endpoint requires authentication."""
    response = requests.get(f"{BASE_URL}/auth/github/status")
    assert response.status_code == 401
    try:
        data = response.json()
        response_data = json.dumps(data, indent=2)
    except (json.JSONDecodeError, ValueError):
        data = None
        response_data = response.text
    print(f"Response data: {response_data}")
    assert data is not None, f"Expected JSON response, got: {response_data}"
    assert "error" in data or "detail" in data


def test_google_oauth_start_requires_auth():
    """Test that Google OAuth start endpoint requires authentication."""
    response = requests.get(f"{BASE_URL}/auth/google/start", allow_redirects=False)
    assert response.status_code == 401
    try:
        data = response.json()
        response_data = json.dumps(data, indent=2)
    except (json.JSONDecodeError, ValueError):
        data = None
        response_data = response.text
    print(f"Response data: {response_data}")
    assert data is not None, f"Expected JSON response, got: {response_data}"
    assert "error" in data or "detail" in data


def test_google_oauth_status_requires_auth():
    """Test that Google OAuth status endpoint requires authentication."""
    response = requests.get(f"{BASE_URL}/auth/google/status")
    assert response.status_code == 401
    try:
        data = response.json()
        response_data = json.dumps(data, indent=2)
    except (json.JSONDecodeError, ValueError):
        data = None
        response_data = response.text
    print(f"Response data: {response_data}")
    assert data is not None, f"Expected JSON response, got: {response_data}"
    assert "error" in data or "detail" in data


def test_providers_status_requires_auth():
    """Test that providers status endpoint requires authentication."""
    response = requests.get(f"{BASE_URL}/auth/providers/status")
    assert response.status_code == 401
    try:
        data = response.json()
        response_data = json.dumps(data, indent=2)
    except (json.JSONDecodeError, ValueError):
        data = None
        response_data = response.text
    print(f"Response data: {response_data}")
    assert data is not None, f"Expected JSON response, got: {response_data}"
    assert "error" in data or "detail" in data

def main():
    """Run all tests via pytest when executed as script."""
    import sys
    print("Starting local API tests...")
    print(f"Testing API at: {BASE_URL}")
    print("\nMake sure the server is running: python run_api.py")
    print("\nRunning tests via pytest...\n")
    
    # Run pytest on this module
    import pytest
    exit_code = pytest.main([__file__, "-v"])
    sys.exit(exit_code)

if __name__ == "__main__":
    main()

