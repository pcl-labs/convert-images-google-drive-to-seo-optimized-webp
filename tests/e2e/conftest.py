"""Pytest configuration for e2e tests against wrangler dev."""

import os
import pytest
import requests
from typing import Optional

# Wrangler dev runs on port 8787 by default
WRANGLER_BASE_URL = os.getenv("WRANGLER_TEST_URL", "http://localhost:8787")
TIMEOUT_SECONDS = float(os.getenv("TEST_HTTP_TIMEOUT", "5"))


def _check_wrangler_available() -> bool:
    """Check if wrangler dev is available, skip test if not."""
    try:
        response = requests.get(f"{WRANGLER_BASE_URL}/health", timeout=TIMEOUT_SECONDS)
        return response.status_code == 200
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return False


@pytest.fixture(scope="session", autouse=True)
def check_wrangler_running():
    """Check if wrangler dev is running before running e2e tests."""
    if not _check_wrangler_available():
        pytest.skip(
            f"Wrangler dev not available at {WRANGLER_BASE_URL}. "
            "Start with: wrangler dev"
        )


@pytest.fixture
def wrangler_client():
    """Provide a requests session configured for wrangler dev."""
    session = requests.Session()
    session.base_url = WRANGLER_BASE_URL
    session.timeout = TIMEOUT_SECONDS
    return session


def make_url(path: str) -> str:
    """Construct full URL for wrangler dev endpoint."""
    path = path.lstrip("/")
    return f"{WRANGLER_BASE_URL}/{path}"

