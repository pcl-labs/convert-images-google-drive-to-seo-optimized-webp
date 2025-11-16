"""
Pytest configuration and fixtures for tests.
Sets up required environment variables for testing.
"""
import os
import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """Set up test environment variables before any tests run."""
    # Capture original value before making any changes
    original = os.environ.get("JWT_SECRET_KEY")
    
    # Set JWT_SECRET_KEY for tests only if it wasn't already set
    if original is None:
        os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-not-for-production"
    
    yield
    
    # Restore original state: delete if it was None, otherwise restore original value
    if original is None:
        os.environ.pop("JWT_SECRET_KEY", None)
    else:
        os.environ["JWT_SECRET_KEY"] = original

