"""
Database utilities for Cloudflare D1 with a local SQLite fallback for development.
"""

import json
import hashlib
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
import logging
import os
import asyncio
import sqlite3
import random

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
                conn = sqlite3.connect(self._sqlite_path, timeout=30, isolation_level='DEFERRED')
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
        conn = sqlite3.connect(self._sqlite_path, timeout=30, isolation_level='DEFERRED')
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_phase1_schema(self, conn: sqlite3.Connection) -> None:
        """Ensure Phase 1 schema: documents table and jobs new columns if missing.
        Safe to run multiple times.
        """
        try:
            cur = conn.cursor()
            # Ensure users table has google_id column for Google login linking
            cur.execute("PRAGMA table_info('users')")
            user_cols = {row[1] for row in cur.fetchall()}
            if 'google_id' not in user_cols:
                cur.execute("ALTER TABLE users ADD COLUMN google_id TEXT")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id)")
            if 'preferences' not in user_cols:
                cur.execute("ALTER TABLE users ADD COLUMN preferences TEXT")
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
            if 'raw_text' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN raw_text TEXT")
            if 'content_format' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN content_format TEXT")
            if 'frontmatter' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN frontmatter TEXT")
            if 'latest_version_id' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN latest_version_id TEXT")
            if 'drive_file_id' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN drive_file_id TEXT")
            if 'drive_revision_id' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN drive_revision_id TEXT")
            if 'drive_folder_id' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN drive_folder_id TEXT")
            if 'drive_drafts_folder_id' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN drive_drafts_folder_id TEXT")
            if 'drive_media_folder_id' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN drive_media_folder_id TEXT")
            if 'drive_published_folder_id' not in doc_cols:
                cur.execute("ALTER TABLE documents ADD COLUMN drive_published_folder_id TEXT")
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
            if 'payload' not in cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN payload TEXT")
            if 'attempt_count' not in cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
            if 'next_attempt_at' not in cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT")
            # Helpful indexes (CREATE INDEX IF NOT EXISTS is idempotent)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_job_type ON jobs(job_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_document_id ON jobs(document_id)")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS drive_workspaces (
                    user_id TEXT PRIMARY KEY,
                    root_folder_id TEXT NOT NULL,
                    drafts_folder_id TEXT NOT NULL,
                    published_folder_id TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS drive_watches (
                    watch_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    drive_file_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    resource_uri TEXT,
                    expires_at TEXT,
                    state TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE,
                    UNIQUE(document_id),
                    UNIQUE(channel_id)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_drive_watches_user ON drive_watches(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_drive_watches_expires_at ON drive_watches(expires_at)")
        except Exception as e:
            # Log full exception details and fail startup to alert operators
            logger.error("Phase 1 schema ensure failed", exc_info=True)
            raise

    # Retention helper: delete step_invocations older than max_age_hours (default 48h)
    async def cleanup_old_step_invocations(self, max_age_hours: int = 48) -> int:
        """Delete step_invocations older than max_age_hours. Returns rows deleted.
        For SQLite/D1 we use datetime('now','-N hours').
        """
        if max_age_hours <= 0:
            max_age_hours = 48
        query = "DELETE FROM step_invocations WHERE created_at < datetime('now', ?)"
        cutoff_param = f"-{int(max_age_hours)} hours"
        # Execute DELETE first and get affected rows from execution result to avoid race conditions.
        # For SQLite, we can fetch changes using cursor.rowcount.
        if self.db and hasattr(self.db, "prepare"):
            # D1 path: execute DELETE and get affected rows from changes count
            try:
                result = await self.db.prepare(query).bind(cutoff_param).run()
                # D1's run() returns result with rows_written property indicating affected rows
                if hasattr(result, 'meta') and hasattr(result.meta, 'rows_written'):
                    rows_written = result.meta.rows_written
                    return int(rows_written) if rows_written is not None else 0
                return 0
            except Exception as e:
                logger.error(f"Cleanup old step_invocations failed (D1): {e}")
                return 0
        try:
            def _delete_and_count():
                conn = self._get_sqlite_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(query, (cutoff_param,))
                    deleted = cur.rowcount if cur.rowcount is not None else 0
                    # Commit if we're in a transaction (DML operations start transactions automatically)
                    if conn.in_transaction:
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
                    # Commit if we're in a transaction (DML operations start transactions automatically)
                    if conn.in_transaction:
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
async def create_user(
    db: Database,
    user_id: str,
    github_id: Optional[str] = None,
    google_id: Optional[str] = None,
    email: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new user."""
    query = """
        INSERT INTO users (user_id, github_id, google_id, email)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            github_id = COALESCE(excluded.github_id, users.github_id),
            google_id = COALESCE(excluded.google_id, users.google_id),
            email = COALESCE(excluded.email, users.email),
            updated_at = datetime('now')
        RETURNING *
    """
    result = await db.execute(query, (user_id, github_id, google_id, email))
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
    payload: Optional[Dict[str, Any]] = None,
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
    payload_json = json.dumps(payload) if payload is not None else None
    query = (
        "INSERT INTO jobs (job_id, user_id, status, progress, drive_folder, extensions, job_type, document_id, output, payload, attempt_count, next_attempt_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL) RETURNING *"
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
            payload_json,
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


async def get_user_by_google_id(db: Database, google_id: str) -> Optional[Dict[str, Any]]:
    """Get user by Google subject identifier."""
    query = "SELECT * FROM users WHERE google_id = ?"
    result = await db.execute(query, (google_id,))
    return dict(result) if result else None


async def get_user_by_email(db: Database, email: str) -> Optional[Dict[str, Any]]:
    """Get user by email address."""
    query = "SELECT * FROM users WHERE email = ?"
    result = await db.execute(query, (email,))
    return dict(result) if result else None


async def delete_user_account(db: Database, user_id: str) -> bool:
    """Permanently delete a user and cascade related records."""
    result = await db.execute("DELETE FROM users WHERE user_id = ? RETURNING user_id", (user_id,))
    return bool(result)


async def update_user_identity(
    db: Database,
    user_id: str,
    *,
    github_id: Optional[str] = None,
    google_id: Optional[str] = None,
    email: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update provider identifiers for a user and return the updated record."""

    assignments = []
    params: list[Any] = []
    if github_id is not None:
        assignments.append("github_id = ?")
        params.append(github_id)
    if google_id is not None:
        assignments.append("google_id = ?")
        params.append(google_id)
    if email is not None:
        assignments.append("email = ?")
        params.append(email)

    if not assignments:
        return await get_user_by_id(db, user_id)

    assignments.append("updated_at = datetime('now')")
    query = f"UPDATE users SET {', '.join(assignments)} WHERE user_id = ? RETURNING *"
    params.append(user_id)
    result = await db.execute(query, tuple(params))
    if result:
        return dict(result)
    return await get_user_by_id(db, user_id)


def _parse_preferences(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def get_user_preferences(db: Database, user_id: str) -> Dict[str, Any]:
    """Return stored user preference blob (defaults to empty dict)."""
    row = await db.execute("SELECT preferences FROM users WHERE user_id = ?", (user_id,))
    if not row:
        return {}
    return _parse_preferences(row.get("preferences"))


async def update_user_preferences(db: Database, user_id: str, preferences: Dict[str, Any]) -> Dict[str, Any]:
    """Persist user preferences JSON blob and return the updated record."""
    payload = json.dumps(preferences or {})
    result = await db.execute(
        "UPDATE users SET preferences = ?, updated_at = datetime('now') WHERE user_id = ? RETURNING *",
        (payload, user_id),
    )
    if result:
        row = dict(result)
        row["preferences"] = _parse_preferences(row.get("preferences"))
        return row
    raise DatabaseError("User not found for preferences update")


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
    
    # For PBKDF2 keys, use targeted lookup via lookup_hash to avoid O(n) full scan
    # Compute lookup_hash using the same logic as when keys are stored
    lookup_hash = hashlib.sha256(api_key.encode()).hexdigest()[:18]  # 9 bytes hex prefix
    candidates = await get_api_key_candidates_by_lookup_hash(db, lookup_hash)
    
    # Filter candidates for PBKDF2 fields (salt and iterations)
    pbkdf2_records = [
        key_record for key_record in candidates
        if key_record.get('salt') is not None and key_record.get('iterations') is not None
    ]
    
    # Return structure with all candidates for caller to verify
    return {'candidates': pbkdf2_records, 'api_key': api_key} if pbkdf2_records else None


# Google OAuth token operations (per integration)
def _decrypt_google_token_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    from .crypto import decrypt
    decoded = dict(row)
    for field in ("access_token", "refresh_token"):
        if decoded.get(field):
            try:
                decoded[field] = decrypt(decoded[field])
            except Exception as e:
                # Log decryption failure with context and return None to prevent processing partial data
                logger.error(
                    f"Failed to decrypt {field} for user_id={decoded.get('user_id')}, "
                    f"integration={decoded.get('integration')}",
                    exc_info=True
                )
                return None
    return decoded


async def list_google_tokens(db: Database, user_id: str) -> List[Dict[str, Any]]:
    """Return all integration tokens for a user."""
    rows = await db.execute_all("SELECT * FROM google_integration_tokens WHERE user_id = ?", (user_id,))
    tokens: List[Dict[str, Any]] = []
    if not rows:
        return tokens
    for row in rows:
        decoded = _decrypt_google_token_row(dict(row))
        if decoded:
            tokens.append(decoded)
    return tokens


async def get_google_token(db: Database, user_id: str, integration: str) -> Optional[Dict[str, Any]]:
    result = await db.execute(
        "SELECT * FROM google_integration_tokens WHERE user_id = ? AND integration = ?",
        (user_id, integration),
    )
    if not result:
        return None
    return _decrypt_google_token_row(dict(result))


async def upsert_google_token(
    db: Database,
    user_id: str,
    integration: str,
    access_token: str,
    refresh_token: Optional[str],
    expiry: Optional[str],
    token_type: Optional[str],
    scopes: Optional[str],
) -> None:
    """Insert or update Google OAuth tokens for a specific integration."""
    from .crypto import encrypt
    token_id = f"{user_id}:{integration}"
    encrypted_access = encrypt(access_token) if access_token else None
    encrypted_refresh = encrypt(refresh_token) if refresh_token else None
    query = """
        INSERT INTO google_integration_tokens (
            token_id, user_id, integration, access_token, refresh_token, expiry, token_type, scopes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(user_id, integration) DO UPDATE SET
            access_token = excluded.access_token,
            refresh_token = COALESCE(excluded.refresh_token, google_integration_tokens.refresh_token),
            expiry = excluded.expiry,
            token_type = excluded.token_type,
            scopes = excluded.scopes,
            updated_at = datetime('now')
    """
    await db.execute(
        query,
        (token_id, user_id, integration, encrypted_access, encrypted_refresh, expiry, token_type, scopes),
    )


async def update_google_token_expiry(
    db: Database,
    user_id: str,
    integration: str,
    access_token: str,
    expiry: Optional[str],
) -> None:
    """Update access token and expiry after refresh."""
    from .crypto import encrypt
    encrypted_access = encrypt(access_token) if access_token else None
    await db.execute(
        "UPDATE google_integration_tokens SET access_token = ?, expiry = ?, updated_at = datetime('now') WHERE user_id = ? AND integration = ?",
        (encrypted_access, expiry, user_id, integration),
    )


async def delete_google_tokens(db: Database, user_id: str, integration: Optional[str] = None) -> None:
    """Delete stored Google OAuth tokens (per integration or all)."""
    if integration:
        await db.execute(
            "DELETE FROM google_integration_tokens WHERE user_id = ? AND integration = ?",
            (user_id, integration),
        )
    else:
        await db.execute("DELETE FROM google_integration_tokens WHERE user_id = ?", (user_id,))

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
        INSERT INTO jobs (job_id, user_id, status, progress, drive_folder, extensions, attempt_count, next_attempt_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
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

    clear_next_attempt_states = {
        JobStatusEnum.PROCESSING.value,
        JobStatusEnum.COMPLETED.value,
        JobStatusEnum.FAILED.value,
        JobStatusEnum.CANCELLED.value,
    }
    if status in clear_next_attempt_states:
        updates.append("next_attempt_at = NULL")

    if status in TERMINAL_JOB_STATES:
        updates.append("completed_at = datetime('now')")
    
    params.append(job_id)
    query = f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?"
    await db.execute(query, tuple(params))


async def set_job_output(db: Database, job_id: str, output: Dict[str, Any]) -> None:
    """Set final job output JSON."""
    await db.execute("UPDATE jobs SET output = ? WHERE job_id = ?", (json.dumps(output), job_id))


async def update_job_retry_state(
    db: Database,
    job_id: str,
    attempt_count: int,
    next_attempt_at: Optional[str],
    error: Optional[str] = None,
) -> None:
    """Persist retry metadata for a job."""
    updates = ["attempt_count = ?"]
    params: list[Any] = [attempt_count]
    updates.append("next_attempt_at = ?")
    params.append(next_attempt_at)
    if error is not None:
        updates.append("error = ?")
        params.append(error)
    params.append(job_id)
    query = f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?"
    await db.execute(query, tuple(params))


async def reset_job_retry_state(db: Database, job_id: str) -> None:
    """Clear retry metadata after a successful run."""
    await db.execute(
        "UPDATE jobs SET attempt_count = 0, next_attempt_at = NULL WHERE job_id = ?",
        (job_id,),
    )


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

async def get_pending_jobs(
    db: Database,
    limit: int = 100,
    statuses: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Get pending or queued jobs that need to be processed.
    
    Args:
        db: Database instance
        limit: Maximum number of jobs to return
        statuses: List of statuses to include (default: ['pending', 'queued'])
    
    Returns:
        List of job dictionaries (excludes cancelled jobs)
    """
    if statuses is None:
        statuses = [JobStatusEnum.PENDING.value, 'queued']
    # If caller passes an empty list, avoid generating IN () and return no rows.
    if not statuses:
        return []
    
    placeholders = ','.join(['?' for _ in statuses])
    query = f"""
        SELECT * FROM jobs
        WHERE status IN ({placeholders})
        AND status != 'cancelled'
        AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))
        ORDER BY created_at ASC
        LIMIT ?
    """
    params = list(statuses) + [limit]
    results = await db.execute_all(query, tuple(params))
    return [dict(row) for row in results] if results else []


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


async def latest_job_by_type(db: Database, user_id: str, job_type: str) -> Optional[Dict[str, Any]]:
    row = await db.execute(
        """
        SELECT * FROM jobs
        WHERE user_id = ? AND job_type = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id, job_type),
    )
    return dict(row) if row else None


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


async def get_drive_workspace(db: Database, user_id: str) -> Optional[Dict[str, Any]]:
    row = await db.execute("SELECT * FROM drive_workspaces WHERE user_id = ?", (user_id,))
    return dict(row) if row else None


async def upsert_drive_workspace(
    db: Database,
    user_id: str,
    root_folder_id: str,
    drafts_folder_id: str,
    published_folder_id: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    meta_json = json.dumps(metadata or {})
    query = (
        """
        INSERT INTO drive_workspaces (user_id, root_folder_id, drafts_folder_id, published_folder_id, metadata)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            root_folder_id=excluded.root_folder_id,
            drafts_folder_id=excluded.drafts_folder_id,
            published_folder_id=excluded.published_folder_id,
            metadata=excluded.metadata,
            updated_at=datetime('now')
        RETURNING *
        """
    )
    row = await db.execute(query, (user_id, root_folder_id, drafts_folder_id, published_folder_id, meta_json))
    return dict(row) if row else {}


async def upsert_drive_watch(
    db: Database,
    *,
    watch_id: str,
    user_id: str,
    document_id: str,
    drive_file_id: str,
    channel_id: str,
    resource_id: str,
    resource_uri: Optional[str],
    expires_at: Optional[str],
    state: str = "active",
) -> Dict[str, Any]:
    query = """
        INSERT INTO drive_watches (watch_id, user_id, document_id, drive_file_id, channel_id, resource_id, resource_uri, expires_at, state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            watch_id=excluded.watch_id,
            channel_id=excluded.channel_id,
            resource_id=excluded.resource_id,
            resource_uri=excluded.resource_uri,
            drive_file_id=excluded.drive_file_id,
            expires_at=excluded.expires_at,
            state=excluded.state,
            updated_at=datetime('now')
        RETURNING *
    """
    row = await db.execute(
        query,
        (
            watch_id,
            user_id,
            document_id,
            drive_file_id,
            channel_id,
            resource_id,
            resource_uri,
            expires_at,
            state,
        ),
    )
    return dict(row) if row else {}


async def delete_drive_watch(
    db: Database,
    *,
    user_id: str,
    document_id: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> None:
    if not user_id:
        raise ValueError("user_id is required to delete drive_watches")
    if not document_id and not channel_id:
        return
    clauses = []
    params: list[Any] = []
    if document_id:
        clauses.append("document_id = ?")
        params.append(document_id)
    if channel_id:
        clauses.append("channel_id = ?")
        params.append(channel_id)
    where = " OR ".join(clauses)
    query = f"DELETE FROM drive_watches WHERE user_id = ? AND ({where})"
    await db.execute(query, tuple([user_id, *params]))


async def get_drive_watch_by_document(db: Database, document_id: str) -> Optional[Dict[str, Any]]:
    row = await db.execute("SELECT * FROM drive_watches WHERE document_id = ?", (document_id,))
    return dict(row) if row else None


async def get_drive_watch_by_channel(db: Database, channel_id: str) -> Optional[Dict[str, Any]]:
    row = await db.execute("SELECT * FROM drive_watches WHERE channel_id = ?", (channel_id,))
    return dict(row) if row else None


async def list_drive_watches_for_user(db: Database, user_id: str) -> List[Dict[str, Any]]:
    rows = await db.execute_all(
        """
        SELECT * FROM drive_watches
        WHERE user_id = ?
        ORDER BY COALESCE(expires_at, '9999-12-31T23:59:59Z') ASC
        """,
        (user_id,),
    )
    return [dict(row) for row in rows or []]


async def list_drive_watches_expiring(
    db: Database,
    *,
    within_seconds: int,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=max(within_seconds, 0))
    cutoff_iso = cutoff.isoformat()
    if user_id:
        rows = await db.execute_all(
            """
            SELECT * FROM drive_watches
            WHERE state = 'active'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
              AND user_id = ?
            ORDER BY expires_at ASC
            """,
            (cutoff_iso, user_id),
        )
    else:
        rows = await db.execute_all(
            """
            SELECT * FROM drive_watches
            WHERE state = 'active'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (cutoff_iso,),
        )
    return [dict(row) for row in rows or []]


async def update_drive_watch_fields(
    db: Database,
    *,
    user_id: str,
    watch_id: str,
    expires_at: Optional[str] = None,
    state: Optional[str] = None,
) -> None:
    assignments = []
    params: list[Any] = []
    if expires_at is not None:
        assignments.append("expires_at = ?")
        params.append(expires_at)
    if state is not None:
        assignments.append("state = ?")
        params.append(state)
    if not assignments:
        return
    assignments.append("updated_at = datetime('now')")
    params.append(watch_id)
    params.append(user_id)
    query = f"UPDATE drive_watches SET {', '.join(assignments)} WHERE watch_id = ? AND user_id = ?"
    await db.execute(query, tuple(params))

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
    drive_file_id: Optional[str] = None,
    drive_revision_id: Optional[str] = None,
    drive_folder_id: Optional[str] = None,
    drive_drafts_folder_id: Optional[str] = None,
    drive_media_folder_id: Optional[str] = None,
    drive_published_folder_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a document row."""
    query = (
        "INSERT INTO documents (document_id, user_id, source_type, source_ref, raw_text, metadata, content_format, frontmatter, latest_version_id, drive_folder_id, drive_drafts_folder_id, drive_media_folder_id, drive_published_folder_id, drive_file_id, drive_revision_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *"
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
            drive_folder_id,
            drive_drafts_folder_id,
            drive_media_folder_id,
            drive_published_folder_id,
            drive_file_id,
            drive_revision_id,
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
    """Update document fields. All updates are applied atomically.
    
    Args:
        db: Database instance
        document_id: Document ID to update
        updates: Dictionary of field updates. Allowed fields: source_type, source_ref, 
                 raw_text, metadata, content_format, frontmatter, latest_version_id
    """
    allowed = {
        "source_type",
        "source_ref",
        "raw_text",
        "metadata",
        "content_format",
        "frontmatter",
        "latest_version_id",
        "drive_file_id",
        "drive_revision_id",
        "drive_folder_id",
        "drive_drafts_folder_id",
        "drive_media_folder_id",
        "drive_published_folder_id",
    }
    fields = []
    params: list[Any] = []
    for k, v in updates.items():
        if k in allowed:
            fields.append(f"{k} = ?")
            if k in {"metadata", "frontmatter"} and v is not None:
                params.append(json.dumps(v))
            else:
                # Include None and empty strings - don't filter them out
                params.append(v)
    if not fields:
        return
    # Use SQLite-compatible datetime (works for both D1 and SQLite)
    fields.append("updated_at = datetime('now')")
    params.append(document_id)
    query = f"UPDATE documents SET {', '.join(fields)} WHERE document_id = ?"
    await db.execute(query, tuple(params))


def _is_unique_constraint_violation(error: Exception) -> bool:
    """Check if an exception represents a UNIQUE constraint violation.
    
    Handles both SQLite and D1 error formats:
    - SQLite: "UNIQUE constraint failed: document_versions.document_id, document_versions.version"
    - D1: May have different formatting but should contain UNIQUE constraint info
    """
    error_str = str(error).upper()
    # Check for UNIQUE constraint patterns
    has_unique = "UNIQUE" in error_str
    has_constraint = "CONSTRAINT" in error_str or "FAILED" in error_str
    # Also check for the specific index name
    has_index_name = "UNIQUE_DOCUMENT_VERSION" in error_str or "DOCUMENT_VERSIONS" in error_str
    
    return (
        (has_unique and has_constraint) or
        (isinstance(error, sqlite3.IntegrityError) and has_unique) or
        has_index_name
    )


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
    """Create immutable document version snapshot.
    
    Handles race conditions in version number assignment by retrying on UNIQUE constraint violations.
    Uses exponential backoff with jitter to avoid thundering herd problems.
    """
    max_retries = 5
    base_delay = 0.01  # 10ms base delay
    
    for attempt in range(max_retries):
        version_id = str(uuid.uuid4())
        try:
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
        except DatabaseError as e:
            # Check if this is a UNIQUE constraint violation (race condition)
            if _is_unique_constraint_violation(e) and attempt < max_retries - 1:
                # Exponential backoff with jitter: base_delay * 2^attempt + random(0, base_delay)
                delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
                logger.warning(
                    f"Version number collision detected for document_id={document_id}, "
                    f"retrying (attempt {attempt + 1}/{max_retries}) after {delay:.3f}s",
                    exc_info=True
                )
                await asyncio.sleep(delay)
                continue
            # Not a constraint violation or max retries reached - re-raise
            raise
        except Exception as e:
            # Check for constraint violations in unwrapped exceptions (e.g., from SQLite)
            if _is_unique_constraint_violation(e) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
                logger.warning(
                    f"Version number collision detected for document_id={document_id}, "
                    f"retrying (attempt {attempt + 1}/{max_retries}) after {delay:.3f}s",
                    exc_info=True
                )
                await asyncio.sleep(delay)
                continue
            # Unexpected error - re-raise
            raise
    
    # Should not reach here, but handle gracefully
    raise DatabaseError(f"Failed to create document version after {max_retries} attempts")


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


async def record_pipeline_event(
    db: Database,
    user_id: str,
    job_id: str,
    event_type: str,
    stage: Optional[str] = None,
    status: Optional[str] = None,
    message: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    *,
    notify_level: Optional[str] = None,
    notify_text: Optional[str] = None,
    notify_context: Optional[Dict[str, Any]] = None,
) -> None:
    event_id = str(uuid.uuid4())
    payload = json.dumps(data or {})
    await db.execute(
        "INSERT INTO pipeline_events (event_id, user_id, job_id, event_type, stage, status, message, data, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (
            event_id,
            user_id,
            job_id,
            event_type,
            stage,
            status,
            message,
            payload,
        ),
    )
    if notify_level:
        context = dict(notify_context or {})
        context.setdefault("job_id", job_id)
        if data and "data" not in context:
            context["data"] = data
        text = notify_text or message or f"{event_type}:{stage} {status}".strip()
        try:
            await create_notification(
                db,
                notif_id=str(uuid.uuid4()),
                user_id=user_id,
                level=notify_level,
                text=text,
                title=None,
                context=context,
                event_id=event_id,
            )
        except Exception as exc:  # pragma: no cover - best-effort notification
            logger.warning(
                "pipeline_event_notification_failed",
                exc_info=True,
                extra={"user_id": user_id, "job_id": job_id, "event_id": event_id, "error": str(exc)},
            )


async def list_pipeline_events(
    db: Database,
    user_id: str,
    *,
    job_id: Optional[str] = None,
    after_sequence: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    query = (
        "SELECT sequence, event_id, user_id, job_id, event_type, stage, status, message, data, created_at "
        "FROM pipeline_events WHERE user_id = ?"
    )
    params: List[Any] = [user_id]
    if job_id:
        query += " AND job_id = ?"
        params.append(job_id)
    if after_sequence is not None:
        query += " AND sequence > ?"
        params.append(after_sequence)
    query += " ORDER BY sequence ASC LIMIT ?"
    params.append(limit)
    rows = await db.execute_all(query, tuple(params))
    events: List[Dict[str, Any]] = []
    for row in rows or []:
        event = dict(row)
        payload = event.get("data")
        if isinstance(payload, str) and payload:
            try:
                event["data"] = json.loads(payload)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.debug(
                    "pipeline_events_json_decode_failed",
                    extra={
                        "sequence": event.get("sequence"),
                        "event_id": event.get("event_id"),
                    },
                )
                event["data"] = {}
        events.append(event)
    return events


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
            f"{job_id}:{event_type}:{datetime.now(timezone.utc).isoformat()}:{uuid.uuid4()}",
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
