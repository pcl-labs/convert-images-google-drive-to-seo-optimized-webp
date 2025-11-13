"""
Pytest configuration and fixtures for tests.
Sets up required environment variables for testing.
"""
import os
import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """Set up test environment variables before any tests run."""
    # Set JWT_SECRET_KEY for tests if not already set
    if "JWT_SECRET_KEY" not in os.environ:
        os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-not-for-production"
    
    yield
    
    # Cleanup: remove test env var if we set it
    if os.environ.get("JWT_SECRET_KEY") == "test-jwt-secret-key-for-testing-only-not-for-production":
        del os.environ["JWT_SECRET_KEY"]

