"""
Database utilities for Cloudflare D1.
"""

import json
import hashlib
from typing import Optional, List, Dict, Any
from datetime import datetime
import logging

from .config import settings
from .models import JobStatus, JobStatusEnum, JobProgress
from .exceptions import DatabaseError

logger = logging.getLogger(__name__)

# Terminal job states - jobs in these states are considered finished
TERMINAL_JOB_STATES = {
    JobStatusEnum.COMPLETED.value,
    JobStatusEnum.FAILED.value,
    JobStatusEnum.CANCELLED.value
}


class Database:
    """Database wrapper for D1 operations."""
    
    def __init__(self, db=None):
        """Initialize database connection."""
        self.db = db or settings.d1_database
    
    async def execute(self, query: str, params: tuple = ()) -> Any:
        """Execute a query."""
        if not self.db:
            raise DatabaseError("Database not initialized")
        try:
            return await self.db.prepare(query).bind(*params).first()
        except Exception as e:
            logger.error(f"Database query failed: {e}", exc_info=True)
            raise DatabaseError(f"Database operation failed: {str(e)}")
    
    async def execute_all(self, query: str, params: tuple = ()) -> List[Any]:
        """Execute a query and return all results."""
        if not self.db:
            raise DatabaseError("Database not initialized")
        try:
            result = await self.db.prepare(query).bind(*params).all()
            return result.results if hasattr(result, 'results') else result
        except Exception as e:
            logger.error(f"Database query failed: {e}", exc_info=True)
            raise DatabaseError(f"Database operation failed: {str(e)}")
    
    async def execute_many(self, query: str, params_list: List[tuple]) -> Any:
        """Execute a query multiple times with different parameters."""
        if not self.db:
            raise DatabaseError("Database not initialized")
        try:
            # D1 doesn't support batch operations directly, so we'll do them sequentially
            results = []
            for params in params_list:
                result = await self.db.prepare(query).bind(*params).run()
                results.append(result)
            return results
        except Exception as e:
            logger.error(f"Database batch operation failed: {e}", exc_info=True)
            raise DatabaseError(f"Database operation failed: {str(e)}")


# User operations
async def create_user(db: Database, user_id: str, github_id: Optional[str] = None, email: Optional[str] = None) -> Dict[str, Any]:
    """Create a new user."""
    query = """
        INSERT INTO users (user_id, github_id, email)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            github_id = COALESCE(excluded.github_id, users.github_id),
            email = COALESCE(excluded.email, users.email),
            updated_at = datetime('now')
        RETURNING *
    """
    result = await db.execute(query, (user_id, github_id, email))
    return dict(result) if result else {}


async def get_user_by_id(db: Database, user_id: str) -> Optional[Dict[str, Any]]:
    """Get user by ID."""
    query = "SELECT * FROM users WHERE user_id = ?"
    result = await db.execute(query, (user_id,))
    return dict(result) if result else None


async def get_user_by_github_id(db: Database, github_id: str) -> Optional[Dict[str, Any]]:
    """Get user by GitHub ID."""
    query = "SELECT * FROM users WHERE github_id = ?"
    result = await db.execute(query, (github_id,))
    return dict(result) if result else None


# API Key operations
async def create_api_key(db: Database, user_id: str, key_hash: str, salt: str, iterations: int, lookup_hash: str) -> None:
    """Create an API key with PBKDF2 hash, salt, iterations, and lookup hash."""
    query = "INSERT INTO api_keys (key_hash, user_id, salt, iterations, lookup_hash) VALUES (?, ?, ?, ?, ?)"
    await db.execute(query, (key_hash, user_id, salt, iterations, lookup_hash))


async def get_api_key_record_by_hash(db: Database, key_hash: str) -> Optional[Dict[str, Any]]:
    """
    Get API key record by hash (for legacy SHA256 keys).
    Returns the API key record with user information.
    
    This function atomically updates last_used and retrieves the user data
    to prevent race conditions where the API key or user could change
    between SELECT and UPDATE operations.
    
    Uses a single UPDATE ... RETURNING statement with subqueries to atomically
    update and retrieve all required data. Only returns a result if the UPDATE
    succeeded (i.e., the key exists).
    """
    # Use UPDATE ... RETURNING with subqueries to get user data atomically
    # This ensures the update and retrieval happen in a single atomic operation
    query = """
        UPDATE api_keys
        SET last_used = datetime('now')
        WHERE key_hash = ?
        RETURNING 
            (SELECT user_id FROM users WHERE user_id = api_keys.user_id) as user_id,
            (SELECT github_id FROM users WHERE user_id = api_keys.user_id) as github_id,
            (SELECT email FROM users WHERE user_id = api_keys.user_id) as email,
            (SELECT created_at FROM users WHERE user_id = api_keys.user_id) as created_at,
            (SELECT updated_at FROM users WHERE user_id = api_keys.user_id) as updated_at,
            api_keys.key_hash,
            api_keys.salt,
            api_keys.iterations,
            api_keys.user_id as api_key_user_id
    """
    result = await db.execute(query, (key_hash,))
    
    # Only return result if the UPDATE actually affected a row (key exists)
    # and the user still exists (user_id subquery returned a value)
    if result and result.get('user_id'):
        return dict(result)
    return None


async def get_all_api_key_records(db: Database) -> List[Dict[str, Any]]:
    """
    Get all API key records with user information.
    Used for API key verification when we need to check against all keys.
    Note: This is less efficient but necessary for PBKDF2 verification.
    For production at scale, consider adding a key_id prefix to API keys.
    """
    query = """
        SELECT u.*, ak.key_hash, ak.salt, ak.iterations, ak.lookup_hash, ak.user_id as api_key_user_id
        FROM users u
        JOIN api_keys ak ON u.user_id = ak.user_id
    """
    results = await db.execute_all(query, ())
    return [dict(row) for row in results] if results else []


async def get_api_key_candidates_by_lookup_hash(db: Database, lookup_hash: str) -> List[Dict[str, Any]]:
    """Get candidate API key records matching a lookup hash (prefix) with user info."""
    query = """
        SELECT u.*, ak.key_hash, ak.salt, ak.iterations, ak.user_id as api_key_user_id
        FROM users u
        JOIN api_keys ak ON u.user_id = ak.user_id
        WHERE ak.lookup_hash = ?
    """
    results = await db.execute_all(query, (lookup_hash,))
    return [dict(row) for row in results] if results else []


async def get_user_by_api_key(db: Database, api_key: str) -> Optional[Dict[str, Any]]:
    """
    Get API key record by raw API key.
    Returns the API key record with user information for verification.
    Supports both PBKDF2 and legacy SHA256 keys.
    
    Note: For PBKDF2 keys, this function returns all candidate records
    for the caller to verify using verify_api_key in auth.py.
    """
    # First, try legacy SHA256 lookup for backward compatibility
    legacy_hash = hashlib.sha256(api_key.encode()).hexdigest()
    legacy_record = await get_api_key_record_by_hash(db, legacy_hash)
    if legacy_record:
        return legacy_record
    
    # For PBKDF2 keys, we need to check all keys
    # This is less efficient but necessary without a key_id prefix
    # In production at scale, consider adding a key_id prefix to API keys
    all_keys = await get_all_api_key_records(db)
    
    # Return all PBKDF2 records for verification in auth.py
    # The caller will verify each one
    pbkdf2_records = [
        key_record for key_record in all_keys
        if key_record.get('salt') is not None and key_record.get('iterations') is not None
    ]
    
    # Return structure with all candidates for caller to verify
    return {'candidates': pbkdf2_records, 'api_key': api_key} if pbkdf2_records else None


# Google OAuth token operations
async def get_google_tokens(db: Database, user_id: str) -> Optional[Dict[str, Any]]:
    """Get stored Google OAuth tokens for a user."""
    query = "SELECT * FROM google_tokens WHERE user_id = ?"
    result = await db.execute(query, (user_id,))
    return dict(result) if result else None


async def upsert_google_tokens(
    db: Database,
    user_id: str,
    access_token: str,
    refresh_token: Optional[str],
    expiry: Optional[str],
    token_type: Optional[str],
    scopes: Optional[str]
) -> None:
    """Insert or update Google OAuth tokens for a user."""
    query = (
        "INSERT INTO google_tokens (user_id, access_token, refresh_token, expiry, token_type, scopes) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "access_token = excluded.access_token, "
        "refresh_token = COALESCE(excluded.refresh_token, google_tokens.refresh_token), "
        "expiry = excluded.expiry, "
        "token_type = excluded.token_type, "
        "scopes = excluded.scopes, "
        "updated_at = datetime('now')"
    )
    await db.execute(query, (user_id, access_token, refresh_token, expiry, token_type, scopes))


async def update_google_tokens_expiry(
    db: Database,
    user_id: str,
    access_token: str,
    expiry: Optional[str]
) -> None:
    """Update access token and expiry after a refresh."""
    query = "UPDATE google_tokens SET access_token = ?, expiry = ?, updated_at = datetime('now') WHERE user_id = ?"
    await db.execute(query, (access_token, expiry, user_id))

# Job operations
async def create_job(
    db: Database,
    job_id: str,
    user_id: str,
    drive_folder: str,
    extensions: List[str]
) -> Dict[str, Any]:
    """Create a new job."""
    query = """
        INSERT INTO jobs (job_id, user_id, status, progress, drive_folder, extensions)
        VALUES (?, ?, ?, ?, ?, ?)
        RETURNING *
    """
    progress = json.dumps({
        "stage": "initializing",
        "downloaded": 0,
        "optimized": 0,
        "skipped": 0,
        "uploaded": 0,
        "deleted": 0,
        "download_failed": 0,
        "upload_failed": 0
    })
    extensions_json = json.dumps(extensions)
    result = await db.execute(
        query,
        (job_id, user_id, JobStatusEnum.PENDING.value, progress, drive_folder, extensions_json)
    )
    return dict(result) if result else {}


async def get_job(db: Database, job_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get a job by ID, optionally filtered by user."""
    if user_id:
        query = "SELECT * FROM jobs WHERE job_id = ? AND user_id = ?"
        result = await db.execute(query, (job_id, user_id))
    else:
        query = "SELECT * FROM jobs WHERE job_id = ?"
        result = await db.execute(query, (job_id,))
    return dict(result) if result else None


async def update_job_status(
    db: Database,
    job_id: str,
    status: str,
    progress: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None
) -> None:
    """Update job status and progress."""
    updates = ["status = ?"]
    params = [status]
    
    if progress is not None:
        updates.append("progress = ?")
        params.append(json.dumps(progress))
    
    if error is not None:
        updates.append("error = ?")
        params.append(error)
    
    if status in TERMINAL_JOB_STATES:
        updates.append("completed_at = datetime('now')")
    
    params.append(job_id)
    query = f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?"
    await db.execute(query, tuple(params))


async def list_jobs(
    db: Database,
    user_id: str,
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None
) -> tuple[List[Dict[str, Any]], int]:
    """List jobs for a user with pagination."""
    offset = (page - 1) * page_size
    where_clause = "WHERE user_id = ?"
    params = [user_id]
    
    if status:
        where_clause += " AND status = ?"
        params.append(status)
    
    # Get total count
    count_query = f"SELECT COUNT(*) as total FROM jobs {where_clause}"
    count_result = await db.execute(count_query, tuple(params))
    total = count_result.get("total", 0) if count_result else 0
    
    # Get jobs
    query = f"""
        SELECT * FROM jobs
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([page_size, offset])
    results = await db.execute_all(query, tuple(params))
    
    jobs = [dict(row) for row in results] if results else []
    return jobs, total


async def get_job_stats(db: Database, user_id: Optional[str] = None) -> Dict[str, int]:
    """Get job statistics."""
    if user_id:
        query = f"""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = '{JobStatusEnum.COMPLETED.value}' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = '{JobStatusEnum.FAILED.value}' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = '{JobStatusEnum.PENDING.value}' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = '{JobStatusEnum.PROCESSING.value}' THEN 1 ELSE 0 END) as processing
            FROM jobs
            WHERE user_id = ?
        """
        result = await db.execute(query, (user_id,))
    else:
        query = f"""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = '{JobStatusEnum.COMPLETED.value}' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = '{JobStatusEnum.FAILED.value}' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = '{JobStatusEnum.PENDING.value}' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = '{JobStatusEnum.PROCESSING.value}' THEN 1 ELSE 0 END) as processing
            FROM jobs
        """
        result = await db.execute(query, ())
    
    return dict(result) if result else {
        "total": 0,
        "completed": 0,
        "failed": 0,
        "pending": 0,
        "processing": 0
    }


async def get_user_count(db: Database) -> int:
    """Get total user count."""
    query = "SELECT COUNT(*) as total FROM users"
    result = await db.execute(query, ())
    return result.get("total", 0) if result else 0

