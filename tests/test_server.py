"""
Test server startup and basic functionality.
These tests verify that the FastAPI application can be imported, initialized, and basic components work.
Endpoint-specific tests are in test_api.py.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client fixture."""
    from src.workers.api.main import app
    return TestClient(app)


def test_app_imports():
    """Test that the application can be imported successfully."""
    from src.workers.api.main import app
    assert app is not None
    assert app.title is not None
    assert hasattr(app, "title")
    assert hasattr(app, "version")


def test_test_client_creation(client):
    """Test that TestClient can be created from the app."""
    assert client is not None
    assert client.app is not None
    assert hasattr(client, "get")
    assert hasattr(client, "post")


def test_app_has_middleware():
    """Test that the app has middleware configured."""
    from src.workers.api.main import app
    # FastAPI stores middleware in app.user_middleware
    assert hasattr(app, "user_middleware")
    assert len(app.user_middleware) > 0


def test_app_has_exception_handlers():
    """Test that the app has exception handlers configured."""
    from src.workers.api.main import app
    assert hasattr(app, "exception_handlers")
    # Should have handlers for APIException and general Exception
    assert len(app.exception_handlers) > 0

