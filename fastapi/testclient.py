"""TestClient stub so tests that import it do not fail on import."""
from __future__ import annotations


class TestClient:  # pragma: no cover - helper only
    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        raise RuntimeError(
            "fastapi.TestClient is unavailable in the lightweight stub. Install FastAPI to use the real TestClient."
        )
