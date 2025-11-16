"""Pytest configuration and fixtures for tests.

This module keeps the environment deterministic and adds a lightweight
``pytest.mark.asyncio`` implementation so we can run async tests without
installing pytest-asyncio (blocked by the sandbox proxy).
"""

from __future__ import annotations

import asyncio
import inspect
import os

from unittest.mock import AsyncMock

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


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run the test inside an event loop")
    if not hasattr(pytest, "AsyncMock"):
        pytest.AsyncMock = AsyncMock


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    marker = pyfuncitem.get_closest_marker("asyncio")
    if marker is None:
        return None

    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None

    loop = asyncio.new_event_loop()
    old_loop = None
    try:
        try:
            old_loop = asyncio.get_event_loop()
        except RuntimeError:
            old_loop = None
        asyncio.set_event_loop(loop)
        argnames = getattr(pyfuncitem._fixtureinfo, "argnames", ()) or ()
        call_kwargs = {name: pyfuncitem.funcargs[name] for name in argnames if name in pyfuncitem.funcargs}
        loop.run_until_complete(func(**call_kwargs))
    finally:
        try:
            asyncio.set_event_loop(old_loop)
        finally:
            loop.close()
    return True

