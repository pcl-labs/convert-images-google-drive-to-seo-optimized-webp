"""
Database utilities for Cloudflare D1 with a local SQLite fallback for development.
"""

import json
import hashlib
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import logging
import os
import asyncio
import sqlite3

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
    """Database wrapper for D1 operations with SQLite fallback."""
    
    def __init__(self, db=None):
        """Initialize database connection.
        If Cloudflare D1 binding is unavailable, use a local SQLite database for development.
        """
        self.db = db or settings.d1_database
        self._sqlite_path: Optional[str] = None
        if not self.db:
            # Local fallback: initialize SQLite in repo directory
            db_path = os.environ.get("LOCAL_SQLITE_PATH", os.path.join(os.getcwd(), "dev.db"))
            self._sqlite_path = db_path
            try:
                # Apply migrations once at startup using a temporary connection
                self._apply_sqlite_migrations()
                logger.info(f"Initialized local SQLite database at {db_path}")
            except Exception as e:
                logger.error(f"Failed to initialize local SQLite database: {e}", exc_info=True)
                raise DatabaseError("Database not initialized") from e
    
    def _apply_sqlite_migrations(self) -> None:
        """Apply migrations from migrations/schema.sql to local SQLite using a temp connection."""
        if not self._sqlite_path:
            return
        try:
            schema_path = os.path.join(os.getcwd(), "migrations", "schema.sql")
            if os.path.exists(schema_path):
                with open(schema_path, "r", encoding="utf-8") as f:
                    sql = f.read()
                conn = sqlite3.connect(self._sqlite_path, timeout=30, isolation_level=None)
                try:
                    conn.row_factory = sqlite3.Row
                    conn.executescript(sql)
                    # Idempotently ensure new Phase 1 schema changes without breaking repeated runs
                    self._ensure_phase1_schema(conn)
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"Applying migrations to SQLite failed: {e}")

    def _get_sqlite_connection(self) -> sqlite3.Connection:
        """Create a new short-lived SQLite connection for each operation."""
        if not self._sqlite_path:
            raise DatabaseError("Database not initialized")
        conn = sqlite3.connect(self._sqlite_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_phase1_schema(self, conn: sqlite3.Connection) -> None:
        """Ensure Phase 1 schema: documents table and jobs new columns if missing.
        Safe to run multiple times.
        """
        try:
            cur = conn.cursor()
            # Ensure documents table
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT,
                    raw_text TEXT,
                    metadata TEXT,
                    content_format TEXT,
                    frontmatter TEXT,
                    latest_version_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_ref)")
            cur.execute("PRAGMA table_info('documents')")
            doc_cols = {row[1] for row in cur.fetchall()}
            if 'content_format' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN content_format TEXT")
            if 'frontmatter' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN frontmatter TEXT")
            if 'latest_version_id' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN latest_version_id TEXT")
            # Document versions table
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS document_versions (
                    version_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    content_format TEXT NOT NULL,
                    frontmatter TEXT,
                    body_mdx TEXT,
                    body_html TEXT,
                    outline TEXT,
                    chapters TEXT,
                    sections TEXT,
                    assets TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    UNIQUE(document_id, version)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_document_versions_document ON document_versions(document_id, version DESC)")
            # Also ensure unique index in case table was created earlier without the constraint
            try:
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_document_version ON document_versions(document_id, version)")
            except Exception:
                pass
            # Document exports table
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS document_exports (
                    export_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','pending','processing','completed','failed','cancelled')),
                    payload TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE,
                    FOREIGN KEY (version_id) REFERENCES document_versions(version_id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_document_exports_document ON document_exports(document_id, created_at DESC)")
            # Trigger to maintain updated_at on updates
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS document_exports_set_updated_at
                AFTER UPDATE ON document_exports
                WHEN NEW.updated_at = OLD.updated_at
                BEGIN
                    UPDATE document_exports SET updated_at = datetime('now') WHERE export_id = OLD.export_id;
                END;
                """
            )

            # Rely on FOREIGN KEY (latest_version_id) for referential integrity; no extra triggers needed
            # Idempotent step invocations
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS step_invocations (
                    idempotency_key TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    step_type TEXT NOT NULL CHECK (step_type IN ('transcript.fetch','outline.generate','chapters.organize','blog.compose','document.persist')),
                    request_hash TEXT NOT NULL,
                    response_body TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (idempotency_key, user_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_step_invocations_user ON step_invocations(user_id, created_at DESC)")
            # Support duplicate detection by request hash
            cur.execute("CREATE INDEX IF NOT EXISTS idx_step_invocations_user_hash ON step_invocations(user_id, request_hash)")
            # Direct lookup by idempotency key
            cur.execute("CREATE INDEX IF NOT EXISTS idx_step_invocations_idempotency_key ON step_invocations(idempotency_key)")

            # Ensure jobs columns exist
            cur.execute("PRAGMA table_info('jobs')")
            cols = {row[1] for row in cur.fetchall()}  # name at index 1
            if 'job_type' not in cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'optimize_drive'")
            if 'document_id' not in cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN document_id TEXT")
            if 'output' not in cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN output TEXT")
            # Helpful indexes (CREATE INDEX IF NOT EXISTS is idempotent)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_job_type ON jobs(job_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_document_id ON jobs(document_id)")
        except Exception as e:
            # Log but do not fail startup; features may be degraded until migration applied
            logger.warning(f"Phase 1 schema ensure failed: {e}")

    # Retention helper: delete step_invocations older than max_age_hours (default 48h)
    async def cleanup_old_step_invocations(self, max_age_hours: int = 48) -> int:
        """Delete step_invocations older than max_age_hours. Returns rows deleted.
        For SQLite/D1 we use datetime('now','-N hours').
        """
        if max_age_hours <= 0:
            max_age_hours = 48
        query = "DELETE FROM step_invocations WHERE created_at < datetime('now', ?)"
        cutoff_param = f"-{int(max_age_hours)} hours"
        # execute() returns first row, so use batch with single statement to get an ack, then count via changes().
        # For SQLite, we can fetch changes using a small helper.
        if self.db and hasattr(self.db, "prepare"):
            # D1 path: estimate deletions with a COUNT beforehand, then delete
            count_query = "SELECT COUNT(*) as cnt FROM step_invocations WHERE created_at < datetime('now', ?)"
            count_result = await self.execute(count_query, (cutoff_param,))
            count = dict(count_result).get("cnt", 0) if count_result else 0
            await self.execute(query, (cutoff_param,))
            return int(count)
        try:
            def _delete_and_count():
                conn = self._get_sqlite_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(query, (cutoff_param,))
                    deleted = cur.rowcount if cur.rowcount is not None else 0
                    if not conn.in_transaction:
                        conn.commit()
                    return deleted
                finally:
                    conn.close()
            return await asyncio.to_thread(_delete_and_count)
        except Exception as e:
            logger.error(f"Cleanup old step_invocations failed: {e}")
            return 0

    # Basic sanitizer to prevent storing PII in response_body
    def _sanitize_response_body(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Redact prohibited keys recursively to avoid storing PII/secrets in step_invocations.
        This is a defense-in-depth measure; callers should avoid including PII.
        """
        prohibited_keys = {
            "email", "access_token", "refresh_token", "token", "password",
            "authorization", "api_key", "key", "secret", "client_secret",
        }

        def _sanitize(value):
            if isinstance(value, dict):
                return {k: ("[REDACTED]" if k.lower() in prohibited_keys else _sanitize(v)) for k, v in value.items()}
            if isinstance(value, list):
                return [_sanitize(v) for v in value]
            return value

        return _sanitize(dict(data or {}))
    
    async def execute(self, query: str, params: tuple = ()) -> Any:
        """Execute a query and return the first row (as dict-like)."""
        if self.db and hasattr(self.db, "prepare"):
            try:
                return await self.db.prepare(query).bind(*params).first()
            except Exception as e:
                logger.error(f"Database query failed: {e}", exc_info=True)
                raise DatabaseError(f"Database operation failed: {str(e)}")
        try:
            def _exec_one():
                conn = self._get_sqlite_connection()
                try:
                    cur = conn.execute(query, params)
                    row = cur.fetchone()
                    if not conn.in_transaction:
                        conn.commit()
                    return row
                finally:
                    conn.close()
            row = await asyncio.to_thread(_exec_one)
            return row
        except Exception as e:
            logger.error(f"SQLite query failed: {e}", exc_info=True)
            raise DatabaseError(f"Database operation failed: {str(e)}")
    
    async def execute_all(self, query: str, params: tuple = ()) -> List[Any]:
        """Execute a query and return all rows."""
        if self.db and hasattr(self.db, "prepare"):
            try:
                result = await self.db.prepare(query).bind(*params).all()
                return result.results if hasattr(result, 'results') else result
            except Exception as e:
                logger.error(f"Database query failed: {e}", exc_info=True)
                raise DatabaseError(f"Database operation failed: {str(e)}")
        try:
            def _exec_all():
                conn = self._get_sqlite_connection()
                try:
                    cur = conn.execute(query, params)
                    rows = cur.fetchall()
                    return rows
                finally:
                    conn.close()
            rows = await asyncio.to_thread(_exec_all)
            return rows
        except Exception as e:
            logger.error(f"SQLite query-all failed: {e}", exc_info=True)
            raise DatabaseError(f"Database operation failed: {str(e)}")
    
    async def execute_many(self, query: str, params_list: List[tuple]) -> Any:
        """Execute a query multiple times with different parameters."""
        if self.db and hasattr(self.db, "prepare"):
            try:
                results = []
                for params in params_list:
                    result = await self.db.prepare(query).bind(*params).run()
                    results.append(result)
                return results
            except Exception as e:
                logger.error(f"Database batch operation failed: {e}", exc_info=True)
                raise DatabaseError(f"Database operation failed: {str(e)}")
        try:
            def _exec_many():
                conn = self._get_sqlite_connection()
                try:
                    cur = conn.cursor()
                    try:
                        # Begin explicit transaction so the whole batch is atomic
                        conn.execute("BEGIN")
                        for params in params_list:
                            cur.execute(query, params)
                        conn.commit()
                        return True
                    except Exception:
                        # Roll back entire batch on any error
                        try:
                            conn.rollback()
                        finally:
                            raise
                finally:
                    conn.close()
            return await asyncio.to_thread(_exec_many)
        except Exception as e:
            logger.error(f"SQLite batch operation failed: {e}", exc_info=True)
            raise DatabaseError(f"Database operation failed: {str(e)}")

    async def batch(self, statements: List[tuple[str, tuple]]):
        """Execute multiple SQL statements atomically.
        For D1, uses db.batch() with prepared statements; for SQLite, wraps in a transaction.
        Each statement is a tuple of (sql, params_tuple).
        """
        if self.db and hasattr(self.db, "prepare") and hasattr(self.db, "batch"):
            try:
                prepared = [self.db.prepare(sql).bind(*(params or ())) for sql, params in statements]
                return await self.db.batch(prepared)
            except Exception as e:
                logger.error(f"D1 batch failed: {e}", exc_info=True)
                raise DatabaseError(f"Database operation failed: {str(e)}")
        # SQLite fallback
        try:
            def _exec_batch():
                conn = self._get_sqlite_connection()
                try:
                    cur = conn.cursor()
                    try:
                        conn.execute("BEGIN")
                        for sql, params in statements:
                            cur.execute(sql, params or ())
                        conn.commit()
                        return True
                    except Exception:
                        try:
                            conn.rollback()
                        finally:
                            raise
                finally:
                    conn.close()
            return await asyncio.to_thread(_exec_batch)
        except Exception as e:
            logger.error(f"SQLite exec batch failed: {e}", exc_info=True)
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
    if not result:
        # If RETURNING doesn't work, fetch the user manually
        logger.warning(f"RETURNING clause didn't return result, fetching user manually: {user_id}")
        return await get_user_by_id(db, user_id) or {}
    return dict(result) if result else {}


async def create_job_extended(
    db: Database,
    job_id: str,
    user_id: str,
    job_type: str,
    document_id: Optional[str] = None,
    output: Optional[Dict[str, Any]] = None,
    drive_folder: Optional[str] = None,
    extensions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a new job with extended fields. Keeps compatibility with existing jobs table.
    Stores output as JSON text when provided.
    """
    progress = json.dumps({
        "stage": "initializing",
        "downloaded": 0,
        "optimized": 0,
        "skipped": 0,
        "uploaded": 0,
        "deleted": 0,
        "download_failed": 0,
        "upload_failed": 0,
        "recent_logs": []
    })
    extensions_json = json.dumps(extensions or [])
    output_json = json.dumps(output) if output is not None else None
    query = (
        "INSERT INTO jobs (job_id, user_id, status, progress, drive_folder, extensions, job_type, document_id, output) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *"
    )
    result = await db.execute(
        query,
        (
            job_id,
            user_id,
            JobStatusEnum.PENDING.value,
            progress,
            drive_folder,
            extensions_json,
            job_type,
            document_id,
            output_json,
        ),
    )
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
    """Get stored Google OAuth tokens for a user (decrypted)."""
    from .crypto import decrypt
    query = "SELECT * FROM google_tokens WHERE user_id = ?"
    result = await db.execute(query, (user_id,))
    if not result:
        return None
    token_dict = dict(result)
    # Decrypt tokens
    if token_dict.get("access_token"):
        try:
            token_dict["access_token"] = decrypt(token_dict["access_token"])
        except Exception:
            # If decryption fails, token might be unencrypted (migration case)
            # Keep as-is for backward compatibility
            pass
    if token_dict.get("refresh_token"):
        try:
            token_dict["refresh_token"] = decrypt(token_dict["refresh_token"])
        except Exception:
            # If decryption fails, token might be unencrypted (migration case)
            # Keep as-is for backward compatibility
            pass
    return token_dict


async def upsert_google_tokens(
    db: Database,
    user_id: str,
    access_token: str,
    refresh_token: Optional[str],
    expiry: Optional[str],
    token_type: Optional[str],
    scopes: Optional[str]
) -> None:
    """Insert or update Google OAuth tokens for a user (encrypted at rest)."""
    from .crypto import encrypt
    # Encrypt tokens before storing
    encrypted_access_token = encrypt(access_token) if access_token else None
    encrypted_refresh_token = encrypt(refresh_token) if refresh_token else None
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
    await db.execute(query, (user_id, encrypted_access_token, encrypted_refresh_token, expiry, token_type, scopes))


async def update_google_tokens_expiry(
    db: Database,
    user_id: str,
    access_token: str,
    expiry: Optional[str]
) -> None:
    """Update access token and expiry after a refresh (encrypted at rest)."""
    from .crypto import encrypt
    # Encrypt token before storing
    encrypted_access_token = encrypt(access_token) if access_token else None
    query = "UPDATE google_tokens SET access_token = ?, expiry = ?, updated_at = datetime('now') WHERE user_id = ?"
    await db.execute(query, (encrypted_access_token, expiry, user_id))


async def delete_google_tokens(db: Database, user_id: str) -> None:
    """Delete stored Google OAuth tokens for a user (disconnect)."""
    query = "DELETE FROM google_tokens WHERE user_id = ?"
    await db.execute(query, (user_id,))

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
        "upload_failed": 0,
        "recent_logs": []
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


async def set_job_output(db: Database, job_id: str, output: Dict[str, Any]) -> None:
    """Set final job output JSON."""
    await db.execute("UPDATE jobs SET output = ? WHERE job_id = ?", (json.dumps(output), job_id))


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
    total = dict(count_result).get("total", 0) if count_result else 0
    
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

async def list_jobs_by_document(
    db: Database,
    user_id: str,
    document_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return recent jobs for a given document."""
    if limit < 1 or limit > 100:
        limit = 50
    rows = await db.execute_all(
        """
        SELECT * FROM jobs
        WHERE user_id = ? AND document_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, document_id, limit),
    )
    return [dict(row) for row in rows] if rows else []


async def list_documents(
    db: Database,
    user_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[List[Dict[str, Any]], int]:
    """List documents for a user with pagination."""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20
    offset = (page - 1) * page_size
    count_row = await db.execute(
        "SELECT COUNT(*) as total FROM documents WHERE user_id = ?",
        (user_id,),
    )
    total = dict(count_row).get("total", 0) if count_row else 0
    rows = await db.execute_all(
        """
        SELECT * FROM documents
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        (user_id, page_size, offset),
    )
    docs = [dict(row) for row in rows] if rows else []
    return docs, total

# Documents operations
async def create_document(
    db: Database,
    document_id: str,
    user_id: str,
    source_type: str,
    source_ref: Optional[str] = None,
    raw_text: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    content_format: Optional[str] = None,
    frontmatter: Optional[Dict[str, Any]] = None,
    latest_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a document row."""
    query = (
        "INSERT INTO documents (document_id, user_id, source_type, source_ref, raw_text, metadata, content_format, frontmatter, latest_version_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *"
    )
    result = await db.execute(
        query,
        (
            document_id,
            user_id,
            source_type,
            source_ref,
            raw_text,
            json.dumps(metadata or {}),
            content_format,
            json.dumps(frontmatter or {}) if frontmatter is not None else None,
            latest_version_id,
        ),
    )
    return dict(result) if result else {}


async def get_document(db: Database, document_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if user_id:
        row = await db.execute("SELECT * FROM documents WHERE document_id = ? AND user_id = ?", (document_id, user_id))
    else:
        row = await db.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,))
    return dict(row) if row else None


async def update_document(
    db: Database,
    document_id: str,
    updates: Dict[str, Any],
) -> None:
    allowed = {"source_type", "source_ref", "raw_text", "metadata", "content_format", "frontmatter", "latest_version_id"}
    fields = []
    params: list[Any] = []
    for k, v in updates.items():
        if k in allowed:
            fields.append(f"{k} = ?")
            if k in {"metadata", "frontmatter"} and v is not None:
                params.append(json.dumps(v))
            else:
                params.append(v)
    if not fields:
        return
    fields.append("updated_at = datetime('now')")
    params.append(document_id)
    await db.execute(f"UPDATE documents SET {', '.join(fields)} WHERE document_id = ?", tuple(params))


async def create_document_version(
    db: Database,
    document_id: str,
    user_id: str,
    content_format: str,
    frontmatter: Dict[str, Any],
    body_mdx: str,
    body_html: str,
    outline: List[Dict[str, Any]],
    chapters: List[Dict[str, Any]],
    sections: List[Dict[str, Any]],
    assets: Dict[str, Any],
) -> Dict[str, Any]:
    """Create immutable document version snapshot."""
    version_id = str(uuid.uuid4())
    result = await db.execute(
        """
        INSERT INTO document_versions (
            version_id, document_id, user_id, version, content_format, frontmatter,
            body_mdx, body_html, outline, chapters, sections, assets
        ) VALUES (
            ?, ?, ?,
            COALESCE((SELECT MAX(version) FROM document_versions WHERE document_id = ?), 0) + 1,
            ?, ?, ?, ?, ?, ?, ?, ?
        )
        RETURNING *
        """,
        (
            version_id,
            document_id,
            user_id,
            document_id,
            content_format,
            json.dumps(frontmatter or {}),
            body_mdx,
            body_html,
            json.dumps(outline or []),
            json.dumps(chapters or []),
            json.dumps(sections or []),
            json.dumps(assets or {}),
        ),
    )
    return dict(result) if result else {}


async def list_document_versions(
    db: Database,
    document_id: str,
    user_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return recent document versions for the user."""
    rows = await db.execute_all(
        """
        SELECT * FROM document_versions
        WHERE document_id = ? AND user_id = ?
        ORDER BY version DESC
        LIMIT ?
        """,
        (document_id, user_id, max(1, min(limit, 50))),
    )
    return [dict(r) for r in rows] if rows else []


async def get_document_version(
    db: Database,
    document_id: str,
    version_id: str,
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """Fetch document version ensuring ownership."""
    row = await db.execute(
        "SELECT * FROM document_versions WHERE document_id = ? AND version_id = ? AND user_id = ?",
        (document_id, version_id, user_id),
    )
    return dict(row) if row else None


async def create_document_export(
    db: Database,
    document_id: str,
    version_id: str,
    user_id: str,
    target: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Record an export request for downstream connectors."""
    export_id = str(uuid.uuid4())
    result = await db.execute(
        """
        INSERT INTO document_exports (export_id, document_id, version_id, user_id, target, status, payload)
        VALUES (?, ?, ?, ?, ?, 'queued', ?)
        RETURNING *
        """,
        (
            export_id,
            document_id,
            version_id,
            user_id,
            target,
            json.dumps(payload or {}),
        ),
    )
    return dict(result) if result else {}


async def get_user_count(db: Database) -> int:
    """Get total user count."""
    query = "SELECT COUNT(*) as total FROM users"
    result = await db.execute(query, ())
    return dict(result).get("total", 0) if result else 0


async def get_step_invocation(
    db: Database,
    user_id: str,
    idempotency_key: str,
) -> Optional[Dict[str, Any]]:
    """Fetch stored step invocation response."""
    row = await db.execute(
        "SELECT * FROM step_invocations WHERE idempotency_key = ? AND user_id = ?",
        (idempotency_key, user_id),
    )
    return dict(row) if row else None


async def save_step_invocation(
    db: Database,
    user_id: str,
    idempotency_key: str,
    step_type: str,
    request_hash: str,
    response_body: Dict[str, Any],
    status_code: int,
) -> None:
    """Persist step invocation response for idempotency."""
    # Optional conflict detection: if an entry exists with a different request_hash, skip insert
    existing = await get_step_invocation(db, user_id, idempotency_key)
    if existing and existing.get("request_hash") != request_hash:
        # Do not overwrite existing different request; service layer already handles 409
        return

    # Sanitize response body to avoid storing PII
    if hasattr(db, "_sanitize_response_body") and callable(db._sanitize_response_body):
        safe_body = db._sanitize_response_body(response_body)
    else:
        safe_body = response_body

    # Idempotent insert: ignore if row already exists for (idempotency_key, user_id)
    await db.execute(
        """
        INSERT OR IGNORE INTO step_invocations (idempotency_key, user_id, step_type, request_hash, response_body, status_code)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            idempotency_key,
            user_id,
            step_type,
            request_hash,
            json.dumps(safe_body),
            int(status_code),
        ),
    )


# Notifications & Events schema and operations
async def ensure_notifications_schema(db: Database) -> None:
    """Create events, notifications, and deliveries tables if they do not exist."""
    # Execute schema creation atomically
    stmts = [
        (
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                payload TEXT,
                occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_events_aggregate ON events(aggregate_type, aggregate_id, occurred_at DESC)", ()),
        (
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                event_id TEXT,
                level TEXT NOT NULL,
                title TEXT,
                text TEXT NOT NULL,
                context TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT
            )
            """,
            (),
        ),
        (
            """
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                notification_id TEXT NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT (datetime('now')),
                seen_at TEXT,
                dismissed_at TEXT
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_notifications_user_created ON notifications(user_id, created_at DESC)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_deliveries_user ON notification_deliveries(user_id, notification_id)", ()),
    ]
    await db.batch(stmts)


async def emit_event(db: Database, evt_id: str, type_: str, aggregate_type: str, aggregate_id: str, payload: dict | None) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO events (id, type, aggregate_type, aggregate_id, payload, occurred_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (evt_id, type_, aggregate_type, aggregate_id, json.dumps(payload or {})),
    )


async def list_usage_events(
    db: Database,
    user_id: str,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """List recent usage events for a user (most recent first)."""
    rows = await db.execute_all(
        "SELECT * FROM usage_events WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    )
    return [dict(r) for r in rows] if rows else []


async def get_usage_summary(
    db: Database,
    user_id: str,
    window_days: int = 7,
) -> Dict[str, Any]:
    """Aggregate a simple summary for a user's usage over the given window (days)."""
    # Aggregate bytes_downloaded, duration_s, events count
    # metrics stored as JSON; extract fields with JSON functions where possible, else sum in Python
    rows = await db.execute_all(
        """
        SELECT metrics, event_type
        FROM usage_events
        WHERE user_id = ? AND created_at >= datetime('now', ?)
        """,
        (user_id, f"-{int(window_days)} days"),
    )
    total_events = 0
    total_bytes = 0
    total_duration = 0.0
    for r in (rows or []):
        total_events += 1
        try:
            metrics_raw = None
            # Support sqlite3.Row, dict, or tuple ordering (metrics, event_type)
            if isinstance(r, dict):
                metrics_raw = r.get("metrics")
            elif hasattr(r, "keys"):
                # sqlite3.Row
                metrics_raw = r["metrics"]
            elif isinstance(r, (list, tuple)):
                metrics_raw = r[0]
            # Parse JSON if needed
            m = metrics_raw
            if isinstance(m, str):
                try:
                    m = json.loads(m)
                except Exception:
                    m = None
            if isinstance(m, dict):
                b = m.get("bytes_downloaded")
                if isinstance(b, int):
                    total_bytes += b
                d = m.get("duration_s")
                if isinstance(d, (int, float)):
                    total_duration += float(d)
        except Exception:
            continue
    return {
        "window_days": int(window_days),
        "events": total_events,
        "bytes_downloaded": total_bytes,
        "audio_duration_s": int(total_duration),
        "minutes_processed": round(total_duration / 60.0, 2),
    }


async def count_usage_events(db: Database, user_id: str) -> int:
    """Return total number of usage events for a user."""
    # Use db.execute (single-row) and adapt to various return shapes
    row = await db.execute(
        "SELECT COUNT(1) AS cnt FROM usage_events WHERE user_id = ?",
        (user_id,),
    )
    try:
        if row is None:
            return 0
        # Some drivers may return a list/iterable for single-row queries
        if isinstance(row, (list, tuple)) and row and not hasattr(row, "keys") and not isinstance(row, dict):
            first = row[0]
            # If the first element is a mapping/row, use it directly
            if isinstance(first, dict):
                return int(first.get("cnt", 0))
            if hasattr(first, "keys"):
                return int(first["cnt"])  # sqlite3.Row-like
            # If it's a scalar or tuple, treat as positional
            return int(first if not isinstance(first, (list, tuple)) else first[0])
        # Mapping-like (dict)
        if isinstance(row, dict):
            return int(row.get("cnt", 0))
        # sqlite3.Row-like
        if hasattr(row, "keys"):
            return int(row["cnt"])  # sqlite3.Row
        # tuple/list fallback
        return int(row[0])
    except Exception:
        return 0

async def create_notification(db: Database, notif_id: str, user_id: str, level: str, text: str, title: str | None = None, context: dict | None = None, event_id: str | None = None) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO notifications (id, user_id, event_id, level, title, text, context, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (notif_id, user_id, event_id, level, title or None, text, json.dumps(context or {})),
    )
    # Also ensure a delivery row exists for the user
    await db.execute(
        "INSERT OR IGNORE INTO notification_deliveries (id, user_id, notification_id, delivered_at) VALUES (?, ?, ?, datetime('now'))",
        (f"{notif_id}:{user_id}", user_id, notif_id),
    )


async def list_notifications(db: Database, user_id: str, after_id: str | None = None, limit: int = 20) -> List[Dict[str, Any]]:
    # Use composite cursor (created_at, id) and include delivery fields via LEFT JOIN
    cursor_created_at: Optional[str] = None
    cursor_id: Optional[str] = None
    if after_id:
        row = await db.execute("SELECT created_at, id FROM notifications WHERE id = ?", (after_id,))
        if row:
            d = dict(row)
            cursor_created_at = d.get("created_at")
            cursor_id = d.get("id")
    query = (
        "SELECT n.*, nd.seen_at, nd.dismissed_at "
        "FROM notifications n "
        "LEFT JOIN notification_deliveries nd ON nd.notification_id = n.id AND nd.user_id = ? "
        "WHERE n.user_id = ?"
    )
    params: List[Any] = [user_id, user_id]
    if cursor_created_at is not None and cursor_id is not None:
        query += " AND (n.created_at > ? OR (n.created_at = ? AND n.id > ?))"
        params.extend([cursor_created_at, cursor_created_at, cursor_id])
    query += " ORDER BY n.created_at DESC, n.id DESC LIMIT ?"
    params.append(limit)
    rows = await db.execute_all(query, tuple(params))
    return [dict(r) for r in rows] if rows else []


async def mark_notification_seen(db: Database, user_id: str, notification_id: str) -> None:
    await db.execute(
        "UPDATE notification_deliveries SET seen_at = datetime('now') WHERE user_id = ? AND notification_id = ?",
        (user_id, notification_id),
    )


async def dismiss_notification(db: Database, user_id: str, notification_id: str) -> None:
    await db.execute(
        "UPDATE notification_deliveries SET dismissed_at = datetime('now') WHERE user_id = ? AND notification_id = ?",
        (user_id, notification_id),
    )


# Usage metering
async def record_usage_event(
    db: Database,
    user_id: str,
    job_id: str,
    event_type: str,
    metrics: Dict[str, Any] | None = None,
) -> None:
    """Record a usage event with metrics JSON."""
    await db.execute(
        "INSERT INTO usage_events (id, user_id, job_id, event_type, metrics, created_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (
            f"{job_id}:{event_type}:{datetime.now(timezone.utc).isoformat()}",
            user_id,
            job_id,
            event_type,
            json.dumps(metrics or {}),
        ),
    )


def map_job_status_to_notification(job: Dict[str, Any]) -> Dict[str, Any] | None:
    """Simple projector: map a job row to a notification payload (level, text)."""
    st = job.get("status", "queued")
    job_id = job.get("job_id")
    if not job_id:
        return None
    # Terminal states only to reduce noise
    if st == "completed":
        return {"level": "success", "text": f"Job {job_id} completed"}
    if st in ("failed", "cancelled"):
        return {"level": "error", "text": f"Job {job_id} {st}"}
    # Suppress non-terminal info-level updates by default
    return None
