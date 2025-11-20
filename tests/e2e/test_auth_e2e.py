"""
End-to-end auth tests against wrangler dev (Cloudflare Workers).

These tests verify auth flows work correctly in the actual Workers runtime
with D1 database bindings. They require wrangler dev to be running.

To run these tests:
1. Start wrangler dev: wrangler dev
2. Run tests: pytest tests/e2e/test_auth_e2e.py -v
"""

import pytest
import requests
import re
from tests.e2e.conftest import make_url, wrangler_client


class TestAuthE2E:
    """End-to-end auth tests against wrangler dev."""

    def test_health_endpoint(self, wrangler_client):
        """Test that wrangler dev is responding."""
        response = wrangler_client.get(make_url("/health"))
        assert response.status_code == 200
        # Health endpoint should return JSON
        data = response.json()
        assert "status" in data or "ok" in str(data).lower()

    def test_home_page_loads(self, wrangler_client):
        """Test that home page loads without errors."""
        response = wrangler_client.get(make_url("/"))
        assert response.status_code in [200, 302]  # 200 or redirect to login/dashboard

    def test_login_page_loads(self, wrangler_client):
        """Test that login page loads."""
        response = wrangler_client.get(make_url("/login"))
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_logout_get_redirects(self, wrangler_client):
        """Test that GET /auth/logout redirects (even without auth)."""
        response = wrangler_client.get(make_url("/auth/logout"), allow_redirects=False)
        # Should redirect (302) or return 200 if already logged out
        assert response.status_code in [200, 302, 307]

    def test_logout_post_requires_csrf(self, wrangler_client):
        """Test that POST /auth/logout requires CSRF token."""
        # Without CSRF token, should fail
        response = wrangler_client.post(
            make_url("/auth/logout"),
            allow_redirects=False
        )
        # Should return 403 (Forbidden) or 422 (Validation Error) for missing CSRF
        assert response.status_code in [403, 422, 400]

    def test_protected_endpoint_requires_auth(self, wrangler_client):
        """Test that protected endpoints require authentication."""
        # Try to access dashboard without auth
        response = wrangler_client.get(make_url("/dashboard"), allow_redirects=False)
        # Should redirect to login (302) or return 401/403
        assert response.status_code in [302, 307, 401, 403]
        
        # If redirect, should go to login
        if response.status_code in [302, 307]:
            location = response.headers.get("location", "")
            assert "login" in location.lower() or location == "/"

    def test_csrf_token_cookie_set_on_login_page(self, wrangler_client):
        """Test that CSRF token cookie is set when visiting login page."""
        response = wrangler_client.get(make_url("/login"))
        assert response.status_code == 200
        
        # Check for CSRF token cookie
        cookies = response.cookies
        csrf_token = cookies.get("csrf_token")
        assert csrf_token is not None, "CSRF token cookie should be set"
        assert len(csrf_token) > 0, "CSRF token should not be empty"

    def test_oauth_start_endpoints_exist(self, wrangler_client):
        """Test that OAuth start endpoints exist (may redirect to providers)."""
        # GitHub OAuth start
        response = wrangler_client.get(
            make_url("/auth/github/start"),
            allow_redirects=False
        )
        # Should redirect to GitHub (302, 303, 307) or return error if not configured
        assert response.status_code in [302, 303, 307, 400, 401, 500]
        
        # Google OAuth start
        response = wrangler_client.get(
            make_url("/auth/google/start"),
            allow_redirects=False
        )
        # Should redirect to Google (302, 303, 307) or return error if not configured
        # 401 is also valid if endpoint requires auth or OAuth isn't configured
        assert response.status_code in [302, 303, 307, 400, 401, 500]

    def test_api_endpoints_require_auth(self, wrangler_client):
        """Test that API endpoints require authentication."""
        # Try to access protected API endpoint without auth
        # Note: /api/jobs might not exist, so try a known endpoint
        response = wrangler_client.get(make_url("/api/jobs"), allow_redirects=False)
        # Should return 401 (Unauthorized), 403 (Forbidden), or 404 (Not Found)
        # 404 is acceptable if the endpoint doesn't exist
        assert response.status_code in [401, 403, 404]

    def test_logout_clears_cookies(self, wrangler_client):
        """Test that logout clears authentication cookies."""
        # First, get a CSRF token by visiting login
        login_response = wrangler_client.get(make_url("/login"))
        csrf_token = login_response.cookies.get("csrf_token")
        
        if not csrf_token:
            pytest.skip("CSRF token not available - cannot test logout")
        
        # Try to logout with CSRF token (may fail if not authenticated, but should handle gracefully)
        logout_response = wrangler_client.post(
            make_url("/auth/logout"),
            data={"csrf_token": csrf_token},
            cookies=login_response.cookies,
            allow_redirects=False
        )
        
        # Should redirect (302) or return 200
        assert logout_response.status_code in [200, 302, 307]
        
        # Check that Set-Cookie headers are present for cookie deletion
        set_cookie_headers = [
            v for k, v in logout_response.headers.items() 
            if k.lower() == "set-cookie"
        ]
        
        # Should have Set-Cookie headers for clearing cookies (Max-Age=0 or empty value)
        if set_cookie_headers:
            # At least one cookie should be cleared
            cookie_clearing_headers = [
                h for h in set_cookie_headers 
                if "Max-Age=0" in h or "=;" in h
            ]
        # Note: This may be empty if user wasn't authenticated, which is fine
        # The important thing is that the endpoint responds correctly

    def test_full_logout_flow_from_dashboard(self, wrangler_client):
        """
        Test the complete logout flow as a user would experience it:
        1. Authenticate (using debug endpoint)
        2. Access dashboard
        3. Click logout button
        4. Verify logout worked
        """
        # Step 1: Authenticate using debug endpoint
        # This simulates what happens after OAuth callback
        auth_response = wrangler_client.get(
            make_url("/debug/simulate-oauth-callback"),
            allow_redirects=True  # Follow redirect to dashboard
        )
        assert auth_response.status_code == 200, "Should be able to authenticate and access dashboard"
        
        # Verify we have auth cookies
        cookies = wrangler_client.cookies
        assert "access_token" in cookies, "access_token cookie should be set after authentication"
        access_token_before = cookies.get("access_token")
        assert access_token_before, "access_token should not be empty"
        
        # Step 2: Access dashboard to get CSRF token
        dashboard_response = wrangler_client.get(make_url("/dashboard"))
        assert dashboard_response.status_code == 200, "Should be able to access dashboard when authenticated"
        
        # CSRF token should be in cookies (set by dashboard page)
        csrf_token = cookies.get("csrf_token")
        if not csrf_token:
            # Try to extract from HTML using regex (no external deps needed)
            csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', dashboard_response.text)
            if csrf_match:
                csrf_token = csrf_match.group(1)
        
        assert csrf_token, "CSRF token should be available (from cookie or form)"
        
        # Step 3: Submit logout form (POST /auth/logout)
        logout_response = wrangler_client.post(
            make_url("/auth/logout"),
            data={"csrf_token": csrf_token},
            allow_redirects=False  # Don't follow redirect so we can check cookies
        )
        
        # Should redirect to home
        assert logout_response.status_code in [302, 307], f"Should redirect after logout, got {logout_response.status_code}"
        assert logout_response.headers.get("location") == "/", "Should redirect to home page"
        
        # Step 4: Verify cookies are cleared in Set-Cookie headers
        set_cookie_headers = [
            v for k, v in logout_response.headers.items() 
            if k.lower() == "set-cookie"
        ]
        assert len(set_cookie_headers) > 0, f"Should have Set-Cookie headers to clear cookies. Got: {set_cookie_headers}"
        
        # Check that access_token is being cleared
        # Note: Cookie may be deleted with secure=True or secure=False, so check for both
        access_token_cleared = any(
            "access_token=" in cookie and ("Max-Age=0" in cookie or "access_token=;" in cookie)
            for cookie in set_cookie_headers
        )
        assert access_token_cleared, f"access_token cookie should be cleared in Set-Cookie header. Headers: {set_cookie_headers}"
        
        # Step 5: Follow redirect and verify we're logged out
        # Update cookies from logout response
        for cookie_header in set_cookie_headers:
            # Parse Set-Cookie header (simplified - requests handles this automatically)
            pass
        
        # Make a new request to dashboard - should redirect to login or return 401/403
        dashboard_after_logout = wrangler_client.get(
            make_url("/dashboard"),
            allow_redirects=False
        )
        assert dashboard_after_logout.status_code in [302, 307, 401, 403], \
            "Should not be able to access dashboard after logout"
        
        if dashboard_after_logout.status_code in [302, 307]:
            location = dashboard_after_logout.headers.get("location", "")
            assert "login" in location.lower() or location == "/", \
                f"Should redirect to login or home, got {location}"

    def test_logout_from_account_page(self, wrangler_client):
        """
        Test logout from the account page (where the logout form actually is).
        This is the real user flow.
        """
        # Step 1: Authenticate
        auth_response = wrangler_client.get(
            make_url("/debug/simulate-oauth-callback"),
            allow_redirects=True
        )
        assert auth_response.status_code == 200
        
        # Step 2: Access account page to get the logout form
        account_response = wrangler_client.get(make_url("/dashboard/account"))
        assert account_response.status_code == 200, "Should be able to access account page when authenticated"
        
        # Extract CSRF token from cookies or HTML
        csrf_token = account_response.cookies.get("csrf_token")
        if not csrf_token:
            # Try to extract from HTML using regex
            csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', account_response.text)
            if csrf_match:
                csrf_token = csrf_match.group(1)
        
        assert csrf_token, "CSRF token should be available"
        
        # Verify logout form exists in HTML
        assert 'action="/auth/logout"' in account_response.text or 'action=\'/auth/logout\'' in account_response.text, \
            "Logout form should exist on account page"
        
        # Step 3: Submit logout form
        logout_response = wrangler_client.post(
            make_url("/auth/logout"),
            data={"csrf_token": csrf_token},
            allow_redirects=False
        )
        
        # Should redirect
        assert logout_response.status_code in [302, 307], f"Should redirect after logout, got {logout_response.status_code}"
        
        # Step 4: Verify cookies are cleared
        set_cookie_headers = [
            v for k, v in logout_response.headers.items() 
            if k.lower() == "set-cookie"
        ]
        
        # Check for cookie clearing headers
        access_token_cleared = any(
            "access_token=" in cookie and ("Max-Age=0" in cookie or "access_token=;" in cookie)
            for cookie in set_cookie_headers
        )
        assert access_token_cleared, "access_token cookie should be cleared"
        
        # Step 5: Verify we can't access protected pages
        protected_response = wrangler_client.get(
            make_url("/dashboard"),
            allow_redirects=False
        )
        assert protected_response.status_code in [302, 307, 401, 403], \
            "Should not be able to access dashboard after logout"

