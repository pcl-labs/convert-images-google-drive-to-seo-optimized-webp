"""Minimal OAuth Flow stub."""
from __future__ import annotations

from typing import Any, Dict, Tuple


class Flow:
    def __init__(self, client_config: Dict[str, Any], scopes: list[str]):
        self.client_config = client_config
        self.scopes = scopes
        self.redirect_uri: str | None = None

    @classmethod
    def from_client_config(cls, client_config: Dict[str, Any], scopes: list[str]):
        return cls(client_config=client_config, scopes=scopes)

    def authorization_url(self, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
        return ("https://example.com/oauth", {})


class InstalledAppFlow(Flow):  # pragma: no cover - helper only
    @classmethod
    def from_client_secrets_file(cls, filename: str, scopes: list[str]):
        return cls(client_config={"installed": {"client_secrets_file": filename}}, scopes=scopes)

    def run_local_server(self, **kwargs: Any):
        class _Creds:
            def __init__(self):
                self.token = ""

        return _Creds()
