"""Stub google.oauth2.credentials module."""
from __future__ import annotations

from typing import List, Optional
from datetime import datetime


class Credentials:
    def __init__(
        self,
        token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        token_uri: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scopes: Optional[List[str]] = None,
        expiry: Optional[datetime] = None,
    ):
        self.token = token
        self.refresh_token = refresh_token
        self.token_uri = token_uri
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes or []
        self.expiry = expiry

    @property
    def valid(self) -> bool:
        return bool(self.token)

    @property
    def expired(self) -> bool:
        if not self.expiry:
            return False
        return self.expiry <= datetime.utcnow()
