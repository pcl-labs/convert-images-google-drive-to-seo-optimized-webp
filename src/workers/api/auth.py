"""
Authentication and authorization utilities.
"""

import secrets
import hashlib
import base64
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlencode
import logging

from .jwt import encode, decode, ExpiredSignatureError, InvalidTokenError

from api.simple_http import AsyncSimpleClient, HTTPStatusError, RequestError


from .config import settings
from .database import (
    Database,
    get_user_by_github_id,
    get_user_by_google_id,
    get_user_by_email,
    create_user,
    update_user_identity,
    create_api_key,
    get_api_key_record_by_hash,
    get_all_api_key_records,
    get_api_key_candidates_by_lookup_hash,
)
from .exceptions import AuthenticationError

logger = logging.getLogger(__name__)

# PBKDF2 configuration
# Use a secure default and allow tuning via configuration
PBKDF2_ITERATIONS = settings.pbkdf2_iterations  # OWASP recommends ~600,000 for PBKDF2-HMAC-SHA256
PBKDF2_SALT_LENGTH = 32  # 32 bytes = 256 bits
PBKDF2_KEY_LENGTH = 32  # 32 bytes = 256 bits


def generate_api_key() -> str:
    """Generate a new API key."""
    return secrets.token_urlsafe(settings.api_key_length)


def hash_api_key(api_key: str) -> Tuple[str, str, int]:
    """
    Hash an API key using PBKDF2-HMAC-SHA256 with a random salt.
    
    Returns:
        Tuple of (key_hash, salt, iterations) where:
        - key_hash: base64-encoded derived key
        - salt: base64-encoded random salt
        - iterations: number of PBKDF2 iterations
    """
    # Generate a random salt for this key
    salt = secrets.token_bytes(PBKDF2_SALT_LENGTH)
    
    # Derive key using PBKDF2-HMAC-SHA256
    key_hash = hashlib.pbkdf2_hmac(
        'sha256',
        api_key.encode('utf-8'),
        salt,
        PBKDF2_ITERATIONS,
        PBKDF2_KEY_LENGTH
    )
    
    # Encode both salt and hash as base64 for storage
    salt_b64 = base64.b64encode(salt).decode('utf-8')
    key_hash_b64 = base64.b64encode(key_hash).decode('utf-8')
    
    return key_hash_b64, salt_b64, PBKDF2_ITERATIONS


def verify_api_key(api_key: str, stored_hash: str, salt: str, iterations: int) -> bool:
    """
    Verify an API key against stored hash using constant-time comparison.
    
    Args:
        api_key: The API key to verify
        stored_hash: Base64-encoded stored hash
        salt: Base64-encoded salt used for the stored hash
        iterations: Number of PBKDF2 iterations used
    
    Returns:
        True if the API key matches, False otherwise
    """
    try:
        # Decode stored salt and hash
        salt_bytes = base64.b64decode(salt)
        stored_hash_bytes = base64.b64decode(stored_hash)
        
        # Derive key from provided API key using same salt and iterations
        derived_hash = hashlib.pbkdf2_hmac(
            'sha256',
            api_key.encode('utf-8'),
            salt_bytes,
            iterations,
            PBKDF2_KEY_LENGTH
        )
        
        # Constant-time comparison to prevent timing attacks
        return secrets.compare_digest(derived_hash, stored_hash_bytes)
    except Exception as e:
        logger.warning(f"API key verification failed: {e}")
        return False


def hash_api_key_legacy(api_key: str) -> str:
    """
    Legacy SHA256 hashing for migration purposes.
    This function is used to identify old-style hashes during migration.
    """
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_jwt_token(
    user_id: str,
    github_id: Optional[str] = None,
    *,
    google_id: Optional[str] = None,
    email: Optional[str] = None,
) -> str:
    """Generate a JWT token for a user."""
    payload = {
        "user_id": user_id,
        "github_id": github_id,
        "google_id": google_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiration_hours),
        "iat": datetime.now(timezone.utc),
    }
    return encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def verify_jwt_token(token: str) -> Dict[str, Any]:
    """Verify and decode a JWT token."""
    try:
        payload = decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm]
        )
        return payload
    except ExpiredSignatureError:
        raise AuthenticationError("Token has expired")
    except InvalidTokenError:
        raise AuthenticationError("Invalid token")


async def get_github_user_info(access_token: str) -> Dict[str, Any]:
    """Get user information from GitHub using access token with robust error handling.
    
    Uses pure HTTP GET request via AsyncSimpleClient (no third-party auth libraries).
    """
    try:
        async with AsyncSimpleClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            try:
                return response.json()
            except Exception as e:
                raise AuthenticationError(f"Failed to parse GitHub user JSON: {e}")
    except HTTPStatusError as e:
        raise AuthenticationError(f"GitHub userinfo HTTP error: {e.response.status_code} {e.response.text}")
    except RequestError as e:
        raise AuthenticationError(f"GitHub userinfo network error: {e}")


async def get_github_primary_email(access_token: str) -> Optional[str]:
    """Fetch the user's primary verified email from GitHub.
    Requires the user:email scope. Returns None if not available.
    """
    try:
        async with AsyncSimpleClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            emails = resp.json()
            # Find primary verified email
            if isinstance(emails, list):
                primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
                if primary and primary.get("email"):
                    return primary["email"]
                # fallback: any verified email
                any_verified = next((e for e in emails if e.get("verified")), None)
                if any_verified and any_verified.get("email"):
                    return any_verified["email"]
    except HTTPStatusError as e:
        logger.warning(f"GitHub emails HTTP error: {e.response.status_code} {e.response.text}")
    except RequestError as e:
        logger.warning(f"GitHub emails network error: {e}")
    except Exception as e:
        logger.warning(f"GitHub emails parse error: {e}")
    return None


async def exchange_github_code(code: str) -> Dict[str, Any]:
    """Exchange GitHub OAuth code for access token.
    
    Uses pure HTTP requests via AsyncSimpleClient (no third-party auth libraries).
    This approach is Cloudflare Workers-compatible and avoids heavy dependencies.
    """
    if not settings.github_client_id or not settings.github_client_secret:
        raise AuthenticationError("GitHub OAuth not configured")
    
    try:
        async with AsyncSimpleClient(timeout=10.0) as client:
            response = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            
            # Parse JSON response
            try:
                json_data = response.json()
            except Exception as e:
                logger.error(f"Failed to parse JSON response from GitHub: {e}")
                raise AuthenticationError("Invalid response format from GitHub OAuth service")
            
            # Check for API-level errors in the JSON response
            if "error" in json_data or "error_description" in json_data:
                error_msg = json_data.get("error_description") or json_data.get("error", "Unknown error")
                raise AuthenticationError(f"GitHub OAuth error: {error_msg}")
            
            return json_data
    except RequestError as e:
        logger.error(f"Network error during GitHub token exchange: {e}")
        raise AuthenticationError("Failed to connect to GitHub OAuth service")
    except HTTPStatusError as e:
        logger.error(f"HTTP error during GitHub token exchange: {e.response.status_code}")
        raise AuthenticationError(f"Failed to exchange GitHub code: HTTP {e.response.status_code}")


async def authenticate_github(db: Database, code: str) -> tuple[str, Dict[str, Any]]:
    """Authenticate user with GitHub OAuth and return JWT token and user info."""
    # Exchange code for access token
    token_data = await exchange_github_code(code)
    access_token = token_data.get("access_token")
    if not access_token:
        raise AuthenticationError("No access token received from GitHub")
    
    # Get user info from GitHub
    github_user = await get_github_user_info(access_token)
    raw_github_id = github_user.get("id")
    if raw_github_id is None:
        raise AuthenticationError("GitHub user id missing in response")
    if not isinstance(raw_github_id, (int, str)):
        raise AuthenticationError("GitHub user id has unexpected type")
    github_id = str(raw_github_id)
    email = github_user.get("email")
    username = github_user.get("login")
    # If email is not present, fetch via /user/emails (requires user:email scope)
    if not email:
        email = await get_github_primary_email(access_token)
    # As a last resort, synthesize a unique noreply email to satisfy NOT NULL UNIQUE
    if not email:
        trimmed = (username or "").strip()
        base_source = trimmed if trimmed else "github"
        base = "".join(base_source.split()).lower()
        if not base:
            base = "github"
        email = f"{base}_{github_id}@users.noreply.github.com"
    
    # Get or create user
    user = await get_user_by_github_id(db, github_id)
    if not user and email:
        existing = await get_user_by_email(db, email)
        if existing:
            user = await update_user_identity(
                db,
                existing["user_id"],
                github_id=github_id,
                email=email,
            )
    if not user:
        # Create new user
        user_id = f"github_{github_id}"
        user = await create_user(db, user_id, github_id=github_id, email=email)
    else:
        user_id = user["user_id"]
        if email and email != user.get("email"):
            updated = await update_user_identity(db, user_id, email=email)
            if updated:
                user = updated

    # Generate JWT token
    jwt_token = generate_jwt_token(
        user_id,
        github_id,
        google_id=user.get("google_id"),
        email=email or user.get("email"),
    )

    return jwt_token, user


def get_google_login_oauth_url(redirect_uri: str) -> Tuple[str, str]:
    """Get Google OAuth authorization URL and state token for login."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise AuthenticationError("Google OAuth not configured")

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": settings.google_client_id,
        "response_type": "code",
        "scope": "openid email",
        "redirect_uri": redirect_uri,
        "state": state,
        "access_type": "online",
        "include_granted_scopes": "false",
    }
    query_string = urlencode(params)
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{query_string}"
    return url, state


async def exchange_google_login_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    """Exchange Google OAuth code for tokens for the login flow.
    
    Uses pure HTTP POST request via AsyncSimpleClient (no google-auth libraries).
    This approach is Cloudflare Workers-compatible and avoids heavy dependencies.
    """
    if not settings.google_client_id or not settings.google_client_secret:
        raise AuthenticationError("Google OAuth not configured")

    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    try:
        # Log the request details for debugging
        logger.debug(
            "Google token exchange request: url=https://oauth2.googleapis.com/token, data keys=%s",
            list(data.keys()),
        )
        async with AsyncSimpleClient(timeout=10.0) as client:
            response = await client.post("https://oauth2.googleapis.com/token", data=data)
            logger.debug(
                "Google token exchange response: status=%s, headers=%s, body_length=%s",
                response.status_code,
                dict(response.headers),
                len(response.content),
            )
            # Log response text for non-200 status codes
            if response.status_code != 200:
                response_text = response.text[:500] if response.text else "(empty)"
                logger.error(
                    "Google token exchange error: status=%s, response_text=%r",
                    response.status_code,
                    response_text,
                )
            response.raise_for_status()
            try:
                return response.json()
            except Exception as exc:
                logger.error("Failed to parse Google token response: %s", exc)
                raise AuthenticationError("Invalid response format from Google OAuth service") from exc
    except HTTPStatusError as exc:
        # Log full response for debugging
        response_body = exc.response.text[:500] if exc.response.text else "(empty)"
        logger.error(
            "HTTP error during Google login token exchange: %s - Response: %s",
            exc.response.status_code,
            response_body,
        )
        raise AuthenticationError(f"Failed to exchange Google code: HTTP {exc.response.status_code} - {response_body}") from exc
    except RequestError as exc:
        logger.error("Network error during Google login token exchange: %s", exc)
        raise AuthenticationError("Failed to connect to Google OAuth service") from exc


async def get_google_user_info(access_token: str) -> Dict[str, Any]:
    """Fetch Google user info via the OpenID Connect userinfo endpoint."""
    try:
        async with AsyncSimpleClient(timeout=10.0) as client:
            response = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            try:
                return response.json()
            except Exception as exc:
                raise AuthenticationError(f"Failed to parse Google user JSON: {exc}") from exc
    except HTTPStatusError as exc:
        raise AuthenticationError(
            f"Google userinfo HTTP error: {exc.response.status_code} {exc.response.text}"
        ) from exc
    except RequestError as exc:
        raise AuthenticationError(f"Google userinfo network error: {exc}") from exc


async def _verify_google_id_token(id_token_value: str) -> Dict[str, Any]:
    """Verify Google ID token via the official tokeninfo endpoint.
    
    Uses pure HTTP GET request to Google's tokeninfo endpoint via AsyncSimpleClient.
    No google-auth library required - verification is done via Google's HTTP API.
    This approach is Cloudflare Workers-compatible.
    """
    try:
        async with AsyncSimpleClient(timeout=10.0) as client:
            response = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": id_token_value},
            )
            response.raise_for_status()
            payload = response.json()
    except HTTPStatusError as exc:
        raise AuthenticationError(
            f"Google tokeninfo HTTP error: {exc.response.status_code} {exc.response.text}"
        ) from exc
    except RequestError as exc:
        raise AuthenticationError(f"Google tokeninfo network error: {exc}") from exc
    audience = payload.get("aud")
    if settings.google_client_id and audience != settings.google_client_id:
        raise AuthenticationError("Google token audience mismatch")
    return payload


async def authenticate_google(
    db: Database,
    code: str,
    redirect_uri: str,
) -> tuple[str, Dict[str, Any]]:
    """Authenticate user with Google OAuth and return JWT token and user info."""

    token_data = await exchange_google_login_code(code, redirect_uri)
    id_token_value = token_data.get("id_token")
    if not id_token_value:
        raise AuthenticationError("No ID token received from Google")

    id_info = await _verify_google_id_token(id_token_value)
    raw_google_id = id_info.get("sub")
    if not raw_google_id:
        raise AuthenticationError("Google user id missing in response")
    google_id = str(raw_google_id)

    email: Optional[str] = None
    if id_info.get("email") and (id_info.get("email_verified") is True):
        email = id_info.get("email")

    access_token = token_data.get("access_token")
    if not email and access_token:
        try:
            userinfo = await get_google_user_info(access_token)
            email_candidate = userinfo.get("email")
            # Extra OIDC robustness: if userinfo has a 'sub', ensure it matches the ID token 'sub'
            sub_matches = (userinfo.get("sub") is None) or (str(userinfo.get("sub")) == google_id)
            if email_candidate and (userinfo.get("email_verified") is True) and sub_matches:
                email = email_candidate
        except AuthenticationError:
            # Continue with fallback email handling below
            pass

    if not email:
        email = f"google_user_{google_id}@accounts.google.com"

    user = await get_user_by_google_id(db, google_id)
    if user:
        user_id = user["user_id"]
        if email and email != user.get("email"):
            updated = await update_user_identity(db, user_id, email=email)
            if updated:
                user = updated
    else:
        existing = await get_user_by_email(db, email) if email else None
        if existing:
            user = await update_user_identity(
                db,
                existing["user_id"],
                google_id=google_id,
                email=email,
            )
            user_id = existing["user_id"]
        else:
            user_id = f"google_{google_id}"
            user = await create_user(db, user_id, google_id=google_id, email=email)

    if not user:
        raise AuthenticationError("Failed to resolve Google user account")

    jwt_token = generate_jwt_token(
        user_id,
        user.get("github_id"),
        google_id=user.get("google_id") or google_id,
        email=email or user.get("email"),
    )

    return jwt_token, user


def _compute_lookup_hash(api_key: str) -> str:
    """Compute a short, indexed lookup hash to target API key candidates (hex prefix)."""
    digest = hashlib.sha256(api_key.encode()).hexdigest()
    return digest[:18]  # 9 bytes hex prefix


async def authenticate_api_key(db: Database, api_key: str) -> Optional[Dict[str, Any]]:
    """
    Authenticate user with API key.
    Supports both new PBKDF2-hashed keys and legacy SHA256-hashed keys for migration.
    """
    # First, try legacy SHA256 lookup for backward compatibility
    legacy_hash = hash_api_key_legacy(api_key)
    legacy_record = await get_api_key_record_by_hash(db, legacy_hash)
    if legacy_record:
        # Legacy key verified - migrate to PBKDF2 (non-blocking best-effort)
        try:
            await migrate_api_key_to_pbkdf2(db, legacy_hash, api_key)
        except Exception:
            pass
        # Return user after successful verification
        return {
            'user_id': legacy_record.get('user_id'),
            'github_id': legacy_record.get('github_id'),
            'email': legacy_record.get('email'),
            'created_at': legacy_record.get('created_at')
        }
    
    # For PBKDF2 keys, perform targeted lookup using lookup_hash
    lookup_hash = _compute_lookup_hash(api_key)
    candidate_keys = await get_api_key_candidates_by_lookup_hash(db, lookup_hash)
    for key_record in candidate_keys:
        stored_hash = key_record.get('key_hash')
        salt = key_record.get('salt')
        iterations = key_record.get('iterations')
        
        # Skip legacy keys (already checked above)
        if salt is None or iterations is None:
            continue
        
        # Verify using PBKDF2
        if verify_api_key(api_key, stored_hash, salt, iterations):
            # Update last_used
            await db.execute(
                "UPDATE api_keys SET last_used = datetime('now') WHERE key_hash = ?",
                (stored_hash,)
            )
            # Return user after successful verification
            return {
                'user_id': key_record.get('user_id'),
                'github_id': key_record.get('github_id'),
                'email': key_record.get('email'),
                'created_at': key_record.get('created_at')
            }
    
    return None


async def migrate_api_key_to_pbkdf2(db: Database, old_hash: str, api_key: str) -> None:
    """
    Migrate a legacy SHA256-hashed API key to PBKDF2.
    This is called when a legacy key is successfully verified.
    
    Migration Strategy:
    - Legacy SHA256 keys are automatically migrated to PBKDF2 on first use after deployment
    - The migration happens transparently during authentication
    - If migration fails, authentication still succeeds (migration is non-blocking)
    - All new API keys are created with PBKDF2 from the start
    - To force migration of all keys, users can regenerate their API keys via the API
    
    This approach ensures:
    1. No service disruption - legacy keys continue to work
    2. Gradual migration - keys are upgraded as they're used
    3. Security improvement - all active keys eventually use PBKDF2
    """
    try:
        # Generate new PBKDF2 hash
        key_hash, salt, iterations = hash_api_key(api_key)
        
        # Compute lookup_hash for targeted queries
        lookup_hash = _compute_lookup_hash(api_key)
        # Update the database record only if it's still legacy (salt IS NULL)
        result = await db.execute(
            "UPDATE api_keys SET key_hash = ?, salt = ?, iterations = ?, lookup_hash = ? WHERE key_hash = ? AND salt IS NULL RETURNING key_hash",
            (key_hash, salt, iterations, lookup_hash, old_hash)
        )
        if result:
            logger.info("Migrated legacy API key to PBKDF2")
        else:
            logger.info("API key already migrated; skipping update")
    except Exception as e:
        logger.error(f"Failed to migrate API key to PBKDF2: {e}", exc_info=True)
        # Don't raise - migration failure shouldn't break authentication


async def create_user_api_key(db: Database, user_id: str) -> str:
    """Create a new API key for a user using PBKDF2."""
    api_key = generate_api_key()
    key_hash, salt, iterations = hash_api_key(api_key)
    lookup_hash = _compute_lookup_hash(api_key)
    await create_api_key(db, user_id, key_hash, salt, iterations, lookup_hash)
    logger.info(f"Created API key for user {user_id}")
    return api_key


def get_github_oauth_url(redirect_uri: str) -> Tuple[str, str]:
    """Get GitHub OAuth authorization URL and state token.
    
    Args:
        redirect_uri: The callback URL to redirect to after OAuth (built from request URL)
    """
    if not settings.github_client_id:
        raise AuthenticationError("GitHub OAuth not configured")
    
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": redirect_uri,
        "scope": "user:email",
        "state": state,
    }
    query_string = urlencode(params)
    url = f"https://github.com/login/oauth/authorize?{query_string}"
    return url, state

