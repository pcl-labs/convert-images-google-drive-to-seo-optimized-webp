"""
Database utilities for Cloudflare D1.
"""

import json
from typing import Optional, List, Dict, Any
from datetime import datetime
import logging

from .config import settings
from .models import JobStatus, JobStatusEnum, JobProgress
from .exceptions import DatabaseError

logger = logging.getLogger(__name__)


class Database:
    """Database wrapper for D1 operations."""
    
    def __init__(self, db=None):
        """Initialize database connection."""
        self.db = db or settings.d1_database
    
    async def execute(self, query: str, params: tuple = ()) -> Any:
        """Execute a query."""
        if not self.db:
            # For local testing, return None instead of raising error
            logger.warning("Database not initialized - returning None for local testing")
            return None
        try:
            return await self.db.prepare(query).bind(*params).first()
        except Exception as e:
            logger.error(f"Database query failed: {e}", exc_info=True)
            raise DatabaseError(f"Database operation failed: {str(e)}")
    
    async def execute_all(self, query: str, params: tuple = ()) -> List[Any]:
        """Execute a query and return all results."""
        if not self.db:
            # For local testing, return empty list
            logger.warning("Database not initialized - returning empty list for local testing")
            return []
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
async def create_api_key(db: Database, user_id: str, key_hash: str) -> None:
    """Create an API key."""
    query = "INSERT INTO api_keys (key_hash, user_id) VALUES (?, ?)"
    await db.execute(query, (key_hash, user_id))


async def get_user_by_api_key(db: Database, key_hash: str) -> Optional[Dict[str, Any]]:
    """Get user by API key hash."""
    query = """
        SELECT u.* FROM users u
        JOIN api_keys ak ON u.user_id = ak.user_id
        WHERE ak.key_hash = ?
    """
    result = await db.execute(query, (key_hash,))
    if result:
        # Update last_used
        await db.execute(
            "UPDATE api_keys SET last_used = datetime('now') WHERE key_hash = ?",
            (key_hash,)
        )
    return dict(result) if result else None


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
        (job_id, user_id, "pending", progress, drive_folder, extensions_json)
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
    
    if status in ["completed", "failed", "cancelled"]:
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
        query = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) as processing
            FROM jobs
            WHERE user_id = ?
        """
        result = await db.execute(query, (user_id,))
    else:
        query = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) as processing
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

