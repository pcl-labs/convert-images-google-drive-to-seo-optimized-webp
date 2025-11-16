"""Minimal OAuth Flow stub."""
from __future__ import annotations

from typing import Any, Dict, Tuple
from urllib.parse import urlencode


class Flow:
    def __init__(self, client_config: Dict[str, Any], scopes: list[str]):
        self.client_config = client_config
        self.scopes = scopes
        self.redirect_uri: str | None = None

    @classmethod
    def from_client_config(cls, client_config: Dict[str, Any], scopes: list[str]):
        return cls(client_config=client_config, scopes=scopes)

    def authorization_url(self, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
        """Generate Google OAuth authorization URL."""
        config = self.client_config.get("web") or self.client_config.get("installed") or {}
        client_id = config.get("client_id")
        auth_uri = config.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth")
        redirect_uris = config.get("redirect_uris") or []
        redirect_uri = self.redirect_uri or (redirect_uris[0] if redirect_uris else None)
        
        if not client_id or not redirect_uri:
            raise ValueError("client_id and redirect_uri are required")
        
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes),
            "response_type": "code",
            "access_type": kwargs.get("access_type", "offline"),
            "prompt": kwargs.get("prompt", "consent"),
            "include_granted_scopes": kwargs.get("include_granted_scopes", "false"),
        }
        
        if "state" in kwargs:
            params["state"] = kwargs["state"]
        
        auth_url = f"{auth_uri}?{urlencode(params)}"
        return (auth_url, {})


class InstalledAppFlow(Flow):  # pragma: no cover - helper only
    @classmethod
    def from_client_secrets_file(cls, filename: str, scopes: list[str]):
        """Stub implementation that records the file path for later handling."""
        return cls(client_config={"installed": {"client_secrets_file": filename}}, scopes=scopes)

    def run_local_server(self, **kwargs: Any):
        class _Creds:
            def __init__(self):
                self.token = ""

        return _Creds()
