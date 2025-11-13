"""
Authentication and authorization utilities.
"""

import secrets
import hashlib
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import jwt
import logging

from .config import settings
from .database import Database, get_user_by_github_id, create_user, get_user_by_api_key, create_api_key
from .exceptions import AuthenticationError

logger = logging.getLogger(__name__)


def generate_api_key() -> str:
    """Generate a new API key."""
    return secrets.token_urlsafe(settings.api_key_length)


def hash_api_key(api_key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_jwt_token(user_id: str, github_id: Optional[str] = None) -> str:
    """Generate a JWT token for a user."""
    payload = {
        "user_id": user_id,
        "github_id": github_id,
        "exp": datetime.utcnow() + timedelta(hours=settings.jwt_expiration_hours),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def verify_jwt_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify and decode a JWT token."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("Token has expired")
    except jwt.InvalidTokenError:
        raise AuthenticationError("Invalid token")


async def get_github_user_info(access_token: str) -> Dict[str, Any]:
    """Get user information from GitHub using access token."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {access_token}"}
        )
        if response.status_code != 200:
            raise AuthenticationError("Failed to get GitHub user info")
        return response.json()


async def exchange_github_code(code: str) -> Dict[str, Any]:
    """Exchange GitHub OAuth code for access token."""
    if not settings.github_client_id or not settings.github_client_secret:
        raise AuthenticationError("GitHub OAuth not configured")
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            raise AuthenticationError("Failed to exchange GitHub code")
        return response.json()


async def authenticate_github(db: Database, code: str) -> tuple[str, Dict[str, Any]]:
    """Authenticate user with GitHub OAuth and return JWT token and user info."""
    # Exchange code for access token
    token_data = await exchange_github_code(code)
    access_token = token_data.get("access_token")
    if not access_token:
        raise AuthenticationError("No access token received from GitHub")
    
    # Get user info from GitHub
    github_user = await get_github_user_info(access_token)
    github_id = str(github_user.get("id"))
    email = github_user.get("email")
    username = github_user.get("login")
    
    # Get or create user
    user = await get_user_by_github_id(db, github_id)
    if not user:
        # Create new user
        user_id = f"github_{github_id}"
        user = await create_user(db, user_id, github_id=github_id, email=email)
    else:
        user_id = user["user_id"]
    
    # Generate JWT token
    jwt_token = generate_jwt_token(user_id, github_id)
    
    return jwt_token, user


async def authenticate_api_key(db: Database, api_key: str) -> Optional[Dict[str, Any]]:
    """Authenticate user with API key."""
    key_hash = hash_api_key(api_key)
    user = await get_user_by_api_key(db, key_hash)
    return user


async def create_user_api_key(db: Database, user_id: str) -> str:
    """Create a new API key for a user."""
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    await create_api_key(db, user_id, key_hash)
    logger.info(f"Created API key for user {user_id}")
    return api_key


def get_github_oauth_url() -> str:
    """Get GitHub OAuth authorization URL."""
    if not settings.github_client_id:
        raise AuthenticationError("GitHub OAuth not configured")
    
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.github_redirect_uri,
        "scope": "user:email",
        "state": secrets.token_urlsafe(16),
    }
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    return f"https://github.com/login/oauth/authorize?{query_string}"

