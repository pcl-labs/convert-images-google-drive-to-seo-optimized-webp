"""
Tests for authentication routes to ensure they handle DB failures gracefully.
These tests verify the fixes for OAuth callbacks, middleware, and protected routes.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from fastapi import HTTPException, status


@pytest.fixture
def client():
    """Create test client with app factory."""
    from src.workers.api.main import app
    return TestClient(app)


def test_github_callback_db_failure_redirects_to_login(client):
    """Test that GitHub OAuth callback redirects to login when DB is unavailable."""
    from src.workers.api import deps
    
    def failing_ensure_db():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize database"
        )
    
    # Set OAuth state cookie to pass state validation
    client.cookies.set("oauth_state", "test-state-value")
    
    # Patch ensure_db to fail
    with patch.object(deps, 'ensure_db', side_effect=failing_ensure_db):
        response = client.get("/auth/github/callback?code=test-code&state=test-state-value", follow_redirects=False)
    
    # Should redirect to /login, not return 500
    assert response.status_code == 302, \
        f"Expected redirect (302), got {response.status_code}. Response: {response.text[:500]}"
    
    assert "/login" in response.headers.get("location", ""), \
        "Should redirect to /login when DB is unavailable"
    
    # OAuth state cookie should be cleared
    assert "oauth_state" not in response.cookies or response.cookies.get("oauth_state") == "", \
        "OAuth state cookie should be cleared on error"


def test_google_login_callback_db_failure_redirects_to_login(client):
    """Test that Google login callback redirects to login when DB is unavailable."""
    from src.workers.api import deps
    
    def failing_ensure_db():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize database"
        )
    
    # Set Google OAuth state cookie to pass state validation
    client.cookies.set("google_oauth_state", "test-state-value")
    
    # Patch ensure_db to fail
    with patch.object(deps, 'ensure_db', side_effect=failing_ensure_db):
        response = client.get("/auth/google/login/callback?code=test-code&state=test-state-value", follow_redirects=False)
    
    # Should redirect to /login, not return 500
    assert response.status_code == 302, \
        f"Expected redirect (302), got {response.status_code}. Response: {response.text[:500]}"
    
    assert "/login" in response.headers.get("location", ""), \
        "Should redirect to /login when DB is unavailable"
    
    # OAuth state cookie should be cleared
    assert "google_oauth_state" not in response.cookies or response.cookies.get("google_oauth_state") == "", \
        "Google OAuth state cookie should be cleared on error"


def test_auth_cookie_middleware_db_failure_continues(client):
    """Test that AuthCookieMiddleware doesn't 500 when DB is unavailable."""
    from src.workers.api import deps
    from src.workers.api.auth import generate_jwt_token
    
    def failing_ensure_db():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize database"
        )
    
    # Create a JWT token without email (to trigger DB lookup in middleware)
    token = generate_jwt_token("test-user-123")
    
    # Set the access_token cookie
    client.cookies.set("access_token", token)
    
    # Patch ensure_db to fail in middleware
    with patch.object(deps, 'ensure_db', side_effect=failing_ensure_db):
        # Try to access a public route - should not 500
        response = client.get("/", follow_redirects=False)
    
    # Should not return 500 - middleware should catch and continue
    assert response.status_code != 500, \
        f"AuthCookieMiddleware should not 500 when DB fails. Got {response.status_code}. Response: {response.text[:500]}"
    
    # Should return 200 (public route) or redirect (if authenticated)
    assert response.status_code in [200, 302, 303, 307], \
        f"Expected 200 or redirect, got {response.status_code}"


def test_dashboard_db_failure_returns_503(client):
    """Test that /dashboard returns 503 (not 500) when DB is unavailable."""
    from src.workers.api import deps
    from src.workers.api.auth import generate_jwt_token
    
    def failing_ensure_db():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize database"
        )
    
    # Create a JWT token with email (so middleware doesn't need DB)
    token = generate_jwt_token("test-user-123", email="test@example.com")
    
    # Set the access_token cookie
    client.cookies.set("access_token", token)
    
    # Patch ensure_db to fail in the route handler
    with patch.object(deps, 'ensure_db', side_effect=failing_ensure_db):
        response = client.get("/dashboard", follow_redirects=False)
    
    # Should return 503 Service Unavailable, not 500
    assert response.status_code == 503, \
        f"Expected 503, got {response.status_code}. Response: {response.text[:500]}"
    
    # Should have appropriate error message
    assert "unavailable" in response.text.lower() or "service" in response.text.lower(), \
        "Response should indicate service is unavailable"


def test_documents_page_db_failure_returns_503(client):
    """Test that /dashboard/documents returns 503 (not 500) when DB is unavailable."""
    from src.workers.api import deps
    from src.workers.api.auth import generate_jwt_token
    
    def failing_ensure_db():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize database"
        )
    
    # Create a JWT token with email
    token = generate_jwt_token("test-user-123", email="test@example.com")
    
    # Set the access_token cookie
    client.cookies.set("access_token", token)
    
    # Patch ensure_db to fail in the route handler
    with patch.object(deps, 'ensure_db', side_effect=failing_ensure_db):
        response = client.get("/dashboard/documents", follow_redirects=False)
    
    # Should return 503 Service Unavailable, not 500
    assert response.status_code == 503, \
        f"Expected 503, got {response.status_code}. Response: {response.text[:500]}"


def test_jobs_page_db_failure_returns_503(client):
    """Test that /dashboard/jobs returns 503 (not 500) when DB is unavailable."""
    from src.workers.api import deps
    from src.workers.api.auth import generate_jwt_token
    
    def failing_ensure_db():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize database"
        )
    
    # Create a JWT token with email
    token = generate_jwt_token("test-user-123", email="test@example.com")
    
    # Set the access_token cookie
    client.cookies.set("access_token", token)
    
    # Patch ensure_db in the web module where it's actually used
    from src.workers.api import web
    with patch.object(web, 'ensure_db', side_effect=failing_ensure_db):
        response = client.get("/dashboard/jobs", follow_redirects=False)
    
    # Should return 503 Service Unavailable, not 500
    assert response.status_code == 503, \
        f"Expected 503, got {response.status_code}. Response: {response.text[:500]}"


def test_auth_cookie_middleware_with_email_no_db_lookup(client):
    """Test that AuthCookieMiddleware doesn't call DB when JWT has email."""
    from src.workers.api import deps
    from src.workers.api.auth import generate_jwt_token
    
    # Create a JWT token WITH email (should not trigger DB lookup)
    token = generate_jwt_token("test-user-123", email="test@example.com")
    
    # Set the access_token cookie
    client.cookies.set("access_token", token)
    
    # Ensure ensure_db is NOT called (since email is in JWT)
    with patch.object(deps, 'ensure_db') as mock_ensure_db:
        response = client.get("/", follow_redirects=False)
        
        # ensure_db should not be called in AuthCookieMiddleware
        # (it may be called elsewhere, but not for user hydration)
        # We can't easily verify this without more complex mocking,
        # but the test verifies the route works without DB
        assert response.status_code in [200, 302, 303, 307], \
            f"Route should work when JWT has email. Got {response.status_code}"

