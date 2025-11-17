"""
Pytest configuration and fixtures for tests.
Sets up required environment variables for testing.
"""
import os
import pytest

# Ensure tests never read the developer's local .env (which can include real API keys)
os.environ.setdefault("PYTEST_DISABLE_DOTENV", "1")
# Disable OpenAI usage during tests to avoid live network calls/costs
os.environ.setdefault("OPENAI_API_KEY", "")

# Capture the original JWT secret once at import time, then ensure a test value
_ORIGINAL_JWT_SECRET = os.environ.get("JWT_SECRET_KEY")
if _ORIGINAL_JWT_SECRET is None:
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-not-for-production"


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """Set up test environment variables before any tests run."""
    # Use the module-level snapshot so we can restore/delete correctly after tests
    yield

    if _ORIGINAL_JWT_SECRET is None:
        os.environ.pop("JWT_SECRET_KEY", None)
    else:
        os.environ["JWT_SECRET_KEY"] = _ORIGINAL_JWT_SECRET
