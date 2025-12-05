"""
Pytest configuration and fixtures for tests.
Sets up required environment variables for testing.
"""
import os
import sys
import uuid
import asyncio
from pathlib import Path
import pytest

from src.workers.api.database import Database, create_user
from src.workers.api.deps import set_db_instance

# Add src/workers to Python path for tests
# This allows absolute imports (from core. and from api.) to work in test environment
# Similar to how main.py and run_api.py set up the path for their environments
_workers_path = Path(__file__).parent.parent / "src" / "workers"
if _workers_path.exists():
    workers_path_str = str(_workers_path.resolve())
    if workers_path_str not in sys.path:
        sys.path.insert(0, workers_path_str)

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


@pytest.fixture(autouse=True)
def clear_proxy_rate_limit_state():
    """Clear proxy rate limiting global state before each test for isolation."""
    # Import here to avoid circular imports
    from src.workers.api import proxy
    
    # Clear the request log and reset cleanup time
    proxy._identity_request_log.clear()
    proxy._identity_last_cleanup = None
    
    yield
    
    # Clean up after test as well
    proxy._identity_request_log.clear()
    proxy._identity_last_cleanup = None


@pytest.fixture
def isolated_db(tmp_path_factory):
    """
    Provide a fresh SQLite database per test by setting LOCAL_SQLITE_PATH
    and wiring it into the global db instance used by ensure_db().
    """
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    original_path = os.environ.get("LOCAL_SQLITE_PATH")
    os.environ["LOCAL_SQLITE_PATH"] = str(db_path)
    
    try:
        db = Database()
        # Ensure the global deps.ensure_db() uses this instance
        set_db_instance(db)
        yield db
    finally:
        if original_path is None:
            os.environ.pop("LOCAL_SQLITE_PATH", None)
        else:
            os.environ["LOCAL_SQLITE_PATH"] = original_path


async def create_test_user(
    db,
    *,
    user_id: str | None = None,
    email: str | None = None,
    github_id: str | None = None,
    google_id: str | None = None,
    **kwargs,
):
    """
    Create a test user with unique provider IDs by default.
    
    This avoids UNIQUE constraint collisions on github_id / google_id
    when tests share the same schema.
    
    This is an async function - use 'await create_test_user(...)' in async test functions,
    or use asyncio.run(create_test_user(...)) in sync test functions.
    """
    if user_id is None:
        user_id = f"user_{uuid.uuid4().hex}"
    
    if email is None:
        email = f"{user_id}@example.com"
    
    if github_id is None:
        github_id = f"github_{uuid.uuid4().hex}"
    
    if google_id is None:
        google_id = f"google_{uuid.uuid4().hex}"
    
    return await create_user(
        db,
        user_id=user_id,
        github_id=github_id,
        google_id=google_id,
        email=email,
        **kwargs,
    )
