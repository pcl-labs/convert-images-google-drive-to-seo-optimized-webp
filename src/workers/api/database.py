"""
Database utilities for Cloudflare D1, with an explicit SQLite path only for tests
or special local runs via the LOCAL_SQLITE_PATH environment variable.
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


def _jsproxy_to_dict(obj: Any) -> Dict[str, Any]:
    """Convert a JsProxy object (from D1/Pyodide) to a Python dict.
    
    D1 query results are returned as JsProxy objects which can't be directly
    converted with dict(). This function handles the conversion by accessing
    properties directly.
    """
    if obj is None:
        return {}
    
    # JsProxy internal properties to exclude
    JS_PROXY_INTERNALS = {'js_id', 'typeof', '__class__', '__dict__', '__module__'}
    
    # Check if it's a JsProxy (from pyodide.ffi)
    try:
        from pyodide.ffi import JsProxy
        if isinstance(obj, JsProxy):
            # JsProxy objects can be accessed like dicts, but we need to iterate
            # over their keys to convert to Python dict
            result = {}
            # Try to get keys - JsProxy objects may have keys() method or be iterable
            try:
                # Try accessing as dict-like object
                if hasattr(obj, 'keys'):
                    for key in obj.keys():
                        key_str = str(key)
                        # Skip JsProxy internal properties
                        if key_str not in JS_PROXY_INTERNALS:
                            result[key_str] = obj[key]
                elif hasattr(obj, '__iter__'):
                    # If it's iterable, try to convert each item
                    for key in obj:
                        key_str = str(key)
                        if key_str not in JS_PROXY_INTERNALS:
                            result[key_str] = obj[key]
                else:
                    # Fallback: try to access common properties
                    # D1 results typically have column names as attributes
                    # Try to get all attributes
                    for attr in dir(obj):
                        if not attr.startswith('_') and attr not in JS_PROXY_INTERNALS:
                            try:
                                value = getattr(obj, attr)
                                # Skip methods
                                if not callable(value):
                                    result[attr] = value
                            except Exception:
                                pass
            except Exception as e:
                logger.debug(f"Error converting JsProxy to dict: {e}")
                # Last resort: try direct dict conversion
                try:
                    converted = dict(obj)
                    # Filter out internals
                    return {k: v for k, v in converted.items() if k not in JS_PROXY_INTERNALS}
                except Exception:
                    return {}
            return result
    except ImportError:
        # pyodide not available (e.g., in tests), try regular dict conversion
        pass
    
    # Not a JsProxy or pyodide not available, try regular conversion
    if isinstance(obj, dict):
        return obj
    try:
        return dict(obj)
    except (TypeError, ValueError):
        return {}


def _jsproxy_to_list(obj: Any) -> List[Any]:
    """Convert a JsProxy array or list to a Python list.
    
    D1 query results from .all() return a result object with a .results property
    that contains an array of JsProxy objects.
    """
    if obj is None:
        return []
    
    # Check if it has a .results property (D1 result object)
    if hasattr(obj, 'results'):
        obj = obj.results
    
    # Check if it's a JsProxy array
    try:
        from pyodide.ffi import JsProxy
        if isinstance(obj, JsProxy):
            # Convert JsProxy array to Python list
            try:
                return [_jsproxy_to_dict(item) if hasattr(item, 'keys') or isinstance(item, JsProxy) else item for item in obj]
            except Exception:
                # Fallback: try to convert directly
                try:
                    return list(obj)
                except Exception:
                    return []
    except ImportError:
        pass
    
    # Not a JsProxy, try regular conversion
    if isinstance(obj, list):
        return obj
    try:
        return list(obj)
    except (TypeError, ValueError):
        return []


def _rows_to_dicts(rows: Any) -> List[Dict[str, Any]]:
    """Normalize D1/SQLite list results into a list of plain dicts."""
    if not rows:
        return []
    rows_list = _jsproxy_to_list(rows)
    return [_jsproxy_to_dict(row) for row in rows_list]


class Database:
    """Database wrapper for D1 operations.
    
    In Cloudflare Workers, a D1 binding (env.DB) is required. SQLite is only
    available when explicitly configured via the LOCAL_SQLITE_PATH environment
    variable (primarily for tests and non-Workers tooling).
    """
    
    def __init__(self, db=None):
        """Initialize database connection.
        - If D1 binding is provided (has 'prepare' method), use D1 (Cloudflare Workers).
        - If LOCAL_SQLITE_PATH is explicitly set, use SQLite (for tests or tools only).
        - Otherwise, raise DatabaseError requiring a D1 binding.
        """
        self.db = db or settings.d1_database
        self._sqlite_path: Optional[str] = None
        
        # Check if we have a D1 binding (Cloudflare Workers)
        is_d1 = self.db and hasattr(self.db, "prepare")
        
        if not is_d1:
            # No D1 binding - only allow SQLite if explicitly set (for tests/tools)
            sqlite_path = os.environ.get("LOCAL_SQLITE_PATH")
            if sqlite_path:
                # SQLite only allowed when explicitly set via LOCAL_SQLITE_PATH (for tests)
                self._sqlite_path = sqlite_path
                self._apply_sqlite_migrations()
            else:
                raise DatabaseError(
                    "Database not configured: D1 binding required. "
                    "For tests, set LOCAL_SQLITE_PATH environment variable."
                )
    
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
            # Note: user_sessions table is created by ensure_sessions_schema() called at app startup
            # No need to duplicate here since ensure_sessions_schema() handles all database types
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


def _serialize_timestamp(value: datetime | str | None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


# User operations
async def create_user(
    db: Database,
    user_id: str,
    github_id: Optional[str] = None,
    google_id: Optional[str] = None,
    email: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new user.
    
    Handles UNIQUE constraint violations on github_id/google_id by checking for existing users first.
    """
    # Check if user already exists by user_id
    existing = await get_user_by_id(db, user_id)
    if existing:
        # User exists - update if needed
        github_id_val = github_id if github_id is not None else ""
        google_id_val = google_id if google_id is not None else ""
        email_val = email if email is not None else ""
        
        query = """
            UPDATE users SET
                github_id = COALESCE(NULLIF(?, ''), github_id),
                google_id = COALESCE(NULLIF(?, ''), google_id),
                email = COALESCE(NULLIF(?, ''), email),
                updated_at = datetime('now')
            WHERE user_id = ?
            RETURNING *
        """
        result = await db.execute(query, (github_id_val, google_id_val, email_val, user_id))
        if result:
            return _jsproxy_to_dict(result) if result else existing
        return existing
    
    # User doesn't exist - check for UNIQUE constraint violations on github_id/google_id
    # D1 doesn't accept Python None - convert to empty string for optional fields
    github_id_val = github_id if github_id is not None else ""
    google_id_val = google_id if google_id is not None else ""
    email_val = email if email is not None else ""
    
    # Check if github_id or google_id would violate UNIQUE constraint
    # Note: Empty strings can also violate UNIQUE constraints, so we check even for empty values
    # However, we only check if the value is non-empty to avoid unnecessary queries
    # The try/except below will catch UNIQUE violations for empty strings
    if github_id_val:
        existing_github = await get_user_by_github_id(db, github_id_val)
        if existing_github and existing_github.get("user_id") != user_id:
            # github_id already exists for a different user - use existing user
            logger.warning(f"github_id {github_id_val} already exists for user {existing_github.get('user_id')}, returning existing user")
            return existing_github
    
    if google_id_val:
        existing_google = await get_user_by_google_id(db, google_id_val)
        if existing_google and existing_google.get("user_id") != user_id:
            # google_id already exists for a different user - use existing user
            logger.warning(f"google_id {google_id_val} already exists for user {existing_google.get('user_id')}, returning existing user")
            return existing_google
    
    # Check if email would violate UNIQUE constraint
    if email_val:
        existing_email = await get_user_by_email(db, email_val)
        if existing_email and existing_email.get("user_id") != user_id:
            # email already exists for a different user - use existing user
            logger.warning(f"email {email_val} already exists for user {existing_email.get('user_id')}, returning existing user")
            return existing_email
    
    # Safe to insert - no conflicts
    query = """
        INSERT INTO users (user_id, github_id, google_id, email)
        VALUES (?, ?, ?, ?)
        RETURNING *
    """
    try:
        result = await db.execute(query, (user_id, github_id_val, google_id_val, email_val))
        if not result:
            # If RETURNING doesn't work, fetch the user manually
            logger.warning(f"RETURNING clause didn't return result, fetching user manually: {user_id}")
            return await get_user_by_id(db, user_id) or {}
        # Convert JsProxy to dict (function is defined earlier in this file)
        return _jsproxy_to_dict(result) if result else {}
    except DatabaseError as e:
        # Handle UNIQUE constraint violations gracefully
        if _is_unique_constraint_violation(e):
            # User might have been created by another request - try to fetch
            logger.warning(f"UNIQUE constraint violation creating user {user_id}, fetching existing user: {e}")
            existing = await get_user_by_id(db, user_id)
            if existing:
                return existing
            # If still not found, check by github_id/google_id/email (including empty strings)
            # Empty strings can also violate UNIQUE constraints, so we check even if the value is empty
            try:
                existing = await get_user_by_github_id(db, github_id_val)
                if existing:
                    return existing
            except Exception:
                pass
            try:
                existing = await get_user_by_google_id(db, google_id_val)
                if existing:
                    return existing
            except Exception:
                pass
            try:
                if email_val:
                    existing = await get_user_by_email(db, email_val)
                    if existing:
                        return existing
            except Exception:
                pass
        # Re-raise if not a UNIQUE constraint violation or user not found
        raise


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
    # D1-safe normalization: no bare None values. Use empty strings for
    # nullable text fields and JSON strings for structured columns.
    drive_folder_val = drive_folder if drive_folder is not None else ""
    document_id_val = document_id if document_id is not None else ""
    extensions_json = json.dumps(extensions or [])
    output_json = json.dumps(output or {}) if output is not None else ""
    payload_json = json.dumps(payload or {}) if payload is not None else ""

    query = (
        "INSERT INTO jobs (job_id, user_id, status, progress, drive_folder, extensions, job_type, document_id, output, payload, attempt_count, next_attempt_at) "
        "VALUES (?, ?, ?, ?, NULLIF(?, ''), ?, ?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), 0, NULL) RETURNING *"
    )
    result = await db.execute(
        query,
        (
            job_id,
            user_id,
            JobStatusEnum.PENDING.value,
            progress,
            drive_folder_val,
            extensions_json,
            job_type,
            document_id_val,
            output_json,
            payload_json,
        ),
    )
    return _jsproxy_to_dict(result) if result else {}


async def get_user_by_id(db: Database, user_id: str) -> Optional[Dict[str, Any]]:
    """Get user by ID."""
    query = "SELECT * FROM users WHERE user_id = ?"
    result = await db.execute(query, (user_id,))
    if not result:
        return None
    # Convert JsProxy to dict
    return _jsproxy_to_dict(result)


async def get_user_by_github_id(db: Database, github_id: str) -> Optional[Dict[str, Any]]:
    """Get user by GitHub ID."""
    query = "SELECT * FROM users WHERE github_id = ?"
    result = await db.execute(query, (github_id,))
    if not result:
        return None
    return _jsproxy_to_dict(result)


async def get_user_by_google_id(db: Database, google_id: str) -> Optional[Dict[str, Any]]:
    """Get user by Google subject identifier."""
    query = "SELECT * FROM users WHERE google_id = ?"
    result = await db.execute(query, (google_id,))
    if not result:
        return None
    return _jsproxy_to_dict(result)


async def get_user_by_email(db: Database, email: str) -> Optional[Dict[str, Any]]:
    """Get user by email address."""
    query = "SELECT * FROM users WHERE email = ?"
    result = await db.execute(query, (email,))
    if not result:
        return None
    return _jsproxy_to_dict(result)


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
        return _jsproxy_to_dict(result)
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
    row_dict = _jsproxy_to_dict(row)
    raw = row_dict.get("preferences")
    return _parse_preferences(raw)


async def update_user_preferences(db: Database, user_id: str, preferences: Dict[str, Any]) -> Dict[str, Any]:
    """Persist user preferences JSON blob and return the updated record."""
    payload = json.dumps(preferences or {})
    result = await db.execute(
        "UPDATE users SET preferences = ?, updated_at = datetime('now') WHERE user_id = ? RETURNING *",
        (payload, user_id),
    )
    if result:
        row = _jsproxy_to_dict(result)
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
    if not result:
        return None
    row = _jsproxy_to_dict(result)
    if row.get("user_id"):
        return row
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
    return _rows_to_dicts(results)


async def get_api_key_candidates_by_lookup_hash(db: Database, lookup_hash: str) -> List[Dict[str, Any]]:
    """Get candidate API key records matching a lookup hash (prefix) with user info."""
    query = """
        SELECT u.*, ak.key_hash, ak.salt, ak.iterations, ak.user_id as api_key_user_id
        FROM users u
        JOIN api_keys ak ON u.user_id = ak.user_id
        WHERE ak.lookup_hash = ?
    """
    results = await db.execute_all(query, (lookup_hash,))
    return _rows_to_dicts(results)


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
# Note: Tokens are stored as plain text - Cloudflare D1 encrypts data at rest automatically


async def list_google_tokens(db: Database, user_id: str) -> List[Dict[str, Any]]:
    """Return all integration tokens for a user."""
    rows = await db.execute_all("SELECT * FROM google_integration_tokens WHERE user_id = ?", (user_id,))
    tokens: List[Dict[str, Any]] = []
    if not rows:
        return tokens
    # Convert JsProxy results to Python list
    rows_list = _jsproxy_to_list(rows)
    for row in rows_list:
        tokens.append(_jsproxy_to_dict(row))
    return tokens


async def get_google_token(db: Database, user_id: str, integration: str) -> Optional[Dict[str, Any]]:
    result = await db.execute(
        "SELECT * FROM google_integration_tokens WHERE user_id = ? AND integration = ?",
        (user_id, integration),
    )
    if not result:
        return None
    # Convert JsProxy to dict
    return _jsproxy_to_dict(result)


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
    """Insert or update Google OAuth tokens for a specific integration.
    
    Tokens are stored as plain text - Cloudflare D1 encrypts data at rest automatically.
    """
    token_id = f"{user_id}:{integration}"
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
        (token_id, user_id, integration, access_token, refresh_token, expiry, token_type, scopes),
    )


async def update_google_token_expiry(
    db: Database,
    user_id: str,
    integration: str,
    access_token: str,
    expiry: Optional[str],
) -> None:
    """Update access token and expiry after refresh.
    
    Tokens are stored as plain text - Cloudflare D1 encrypts data at rest automatically.
    """
    await db.execute(
        "UPDATE google_integration_tokens SET access_token = ?, expiry = ?, updated_at = datetime('now') WHERE user_id = ? AND integration = ?",
        (access_token, expiry, user_id, integration),
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
    return _jsproxy_to_dict(result) if result else {}


async def get_job(db: Database, job_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get a job by ID, optionally filtered by user."""
    if user_id:
        query = "SELECT * FROM jobs WHERE job_id = ? AND user_id = ?"
        result = await db.execute(query, (job_id, user_id))
    else:
        query = "SELECT * FROM jobs WHERE job_id = ?"
        result = await db.execute(query, (job_id,))
    return _jsproxy_to_dict(result) if result else None


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
    if count_result:
        count_dict = _jsproxy_to_dict(count_result)
        total = count_dict.get("total", 0)
    else:
        total = 0
    
    # Get jobs
    query = f"""
        SELECT * FROM jobs
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([page_size, offset])
    results = await db.execute_all(query, tuple(params))
    
    jobs = _rows_to_dicts(results)
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
    
    if not result:
        return {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "pending": 0,
            "processing": 0,
        }
    stats = _jsproxy_to_dict(result)
    return {
        "total": stats.get("total", 0) or 0,
        "completed": stats.get("completed", 0) or 0,
        "failed": stats.get("failed", 0) or 0,
        "pending": stats.get("pending", 0) or 0,
        "processing": stats.get("processing", 0) or 0,
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
    return _rows_to_dicts(results)


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
    return _rows_to_dicts(rows)


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
    return _jsproxy_to_dict(row) if row else None


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
    if count_row:
        count_dict = _jsproxy_to_dict(count_row)
        total = count_dict.get("total", 0)
    else:
        total = 0
    rows = await db.execute_all(
        """
        SELECT * FROM documents
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        (user_id, page_size, offset),
    )
    if not rows:
        return [], total
    # Convert JsProxy results to Python list
    rows_list = _jsproxy_to_list(rows)
    docs = [_jsproxy_to_dict(row) for row in rows_list]
    return docs, total


async def get_drive_workspace(db: Database, user_id: str) -> Optional[Dict[str, Any]]:
    row = await db.execute("SELECT * FROM drive_workspaces WHERE user_id = ?", (user_id,))
    if not row:
        return None
    return _jsproxy_to_dict(row)


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
    if not row:
        return {}
    return _jsproxy_to_dict(row)


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
    if not row:
        return {}
    return _jsproxy_to_dict(row)


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
    if not row:
        return None
    return _jsproxy_to_dict(row)


async def get_drive_watch_by_channel(db: Database, channel_id: str) -> Optional[Dict[str, Any]]:
    row = await db.execute("SELECT * FROM drive_watches WHERE channel_id = ?", (channel_id,))
    if not row:
        return None
    return _jsproxy_to_dict(row)


async def list_drive_watches_for_user(db: Database, user_id: str) -> List[Dict[str, Any]]:
    rows = await db.execute_all(
        """
        SELECT * FROM drive_watches
        WHERE user_id = ?
        ORDER BY COALESCE(expires_at, '9999-12-31T23:59:59Z') ASC
        """,
        (user_id,),
    )
    if not rows:
        return []
    # Convert JsProxy results to Python list
    rows_list = _jsproxy_to_list(rows)
    return [_jsproxy_to_dict(row) for row in rows_list]


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
    if not rows:
        return []
    # Convert JsProxy results to Python list
    rows_list = _jsproxy_to_list(rows)
    return [_jsproxy_to_dict(row) for row in rows_list]


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
    # D1 doesn't accept Python None values directly; convert optionals to empty
    # strings and use NULLIF in SQL where NULL is desired (see gotchas doc).
    source_ref_val = source_ref if source_ref is not None else ""
    raw_text_val = raw_text if raw_text is not None else ""
    content_format_val = content_format if content_format is not None else ""
    # Always store metadata/frontmatter as JSON text (never None)
    metadata_text = json.dumps(metadata or {})
    frontmatter_text = json.dumps(frontmatter or {})
    latest_version_id_val = latest_version_id if latest_version_id is not None else ""
    drive_folder_id_val = drive_folder_id if drive_folder_id is not None else ""
    drive_drafts_folder_id_val = drive_drafts_folder_id if drive_drafts_folder_id is not None else ""
    drive_media_folder_id_val = drive_media_folder_id if drive_media_folder_id is not None else ""
    drive_published_folder_id_val = drive_published_folder_id if drive_published_folder_id is not None else ""
    drive_file_id_val = drive_file_id if drive_file_id is not None else ""
    drive_revision_id_val = drive_revision_id if drive_revision_id is not None else ""

    query = (
        "INSERT INTO documents (document_id, user_id, source_type, source_ref, raw_text, metadata, content_format, frontmatter, latest_version_id, drive_folder_id, drive_drafts_folder_id, drive_media_folder_id, drive_published_folder_id, drive_file_id, drive_revision_id) "
        "VALUES (?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), ?, NULLIF(?, ''), ?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, '')) RETURNING *"
    )
    result = await db.execute(
        query,
        (
            document_id,
            user_id,
            source_type,
            source_ref_val,
            raw_text_val,
            metadata_text,
            content_format_val,
            frontmatter_text,
            latest_version_id_val,
            drive_folder_id_val,
            drive_drafts_folder_id_val,
            drive_media_folder_id_val,
            drive_published_folder_id_val,
            drive_file_id_val,
            drive_revision_id_val,
        ),
    )
    # D1 returns JsProxy rows; normalize to plain dict using helper
    return _jsproxy_to_dict(result) if result else {}


async def get_document(db: Database, document_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if user_id:
        row = await db.execute("SELECT * FROM documents WHERE document_id = ? AND user_id = ?", (document_id, user_id))
    else:
        row = await db.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,))
    if not row:
        return None
    # D1 returns JsProxy objects; normalize to plain dict. For SQLite this is
    # also safe because _jsproxy_to_dict falls back to regular dict conversion.
    return _jsproxy_to_dict(row)


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
            return _jsproxy_to_dict(result) if result else {}
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
    return _rows_to_dicts(rows)


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
    return _jsproxy_to_dict(row) if row else None


async def update_document_latest_version_if_match(
    db: Database,
    document_id: str,
    expected_version_id: Optional[str],
    new_version_id: str,
) -> bool:
    """Optimistically update documents.latest_version_id when the current value matches expected.

    Returns True if latest_version_id ends up set to new_version_id, False otherwise.
    Works for both D1 and SQLite via the Database wrapper.
    """
    await db.execute(
        """
        UPDATE documents
        SET latest_version_id = ?, updated_at = datetime('now')
        WHERE document_id = ? AND (latest_version_id IS ? OR latest_version_id = ?)
        """,
        (new_version_id, document_id, expected_version_id, expected_version_id),
    )

    row = await db.execute(
        "SELECT latest_version_id FROM documents WHERE document_id = ?",
        (document_id,),
    )
    if not row:
        return False
    doc = _jsproxy_to_dict(row)
    return doc.get("latest_version_id") == new_version_id


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
    return _jsproxy_to_dict(result) if result else {}


async def get_user_count(db: Database) -> int:
    """Get total user count."""
    query = "SELECT COUNT(*) as total FROM users"
    result = await db.execute(query, ())
    if not result:
        return 0
    count = _jsproxy_to_dict(result)
    return int(count.get("total", 0) or 0)


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
    return _jsproxy_to_dict(row) if row else None


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


async def ensure_sessions_schema(db: Database) -> None:
    """Ensure the user_sessions table exists for session tracking."""
    stmts = [
        (
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                last_notification_id TEXT,
                ip_address TEXT,
                user_agent TEXT,
                revoked_at TEXT,
                extra TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """,
            (),
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id, created_at DESC)",
            (),
        ),
    ]
    await db.batch(stmts)


async def ensure_full_schema(db: Database) -> None:
    """Apply the full database schema from migrations/schema.sql to D1.
    
    This function applies all tables, indexes, and triggers defined in the schema.
    All statements use IF NOT EXISTS, so it's safe to run multiple times.
    """
    logger.info("Applying full database schema to D1")
    
    # Split schema into individual statements and execute them
    # We'll apply the schema in logical groups to handle dependencies
    
    # Core tables first (users must exist before others due to foreign keys)
    core_tables = [
        (
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                github_id TEXT UNIQUE,
                google_id TEXT UNIQUE,
                email TEXT NOT NULL UNIQUE,
                preferences TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            (),
        ),
        ("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_users_github_id ON users(github_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id)", ()),
    ]
    
    # API keys table
    api_keys_tables = [
        (
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                key_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_used TEXT,
                salt TEXT,
                iterations INTEGER,
                lookup_hash TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_api_keys_lookup_hash ON api_keys(lookup_hash)", ()),
    ]
    
    # Jobs table
    jobs_tables = [
        (
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
                progress TEXT NOT NULL DEFAULT '{}',
                drive_folder TEXT,
                extensions TEXT,
                job_type TEXT NOT NULL DEFAULT 'optimize_drive',
                document_id TEXT,
                output TEXT,
                payload TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT,
                error TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES documents(document_id)
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_jobs_status_next_attempt ON jobs(status, next_attempt_at)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_jobs_job_type ON jobs(job_type)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_jobs_document_id ON jobs(document_id)", ()),
    ]
    
    # Jobs triggers
    jobs_triggers = [
        (
            """
            CREATE TRIGGER IF NOT EXISTS check_job_status 
            BEFORE INSERT ON jobs
            WHEN NEW.status NOT IN ('pending', 'processing', 'completed', 'failed', 'cancelled')
            BEGIN
                SELECT RAISE(ABORT, 'Invalid status value. Must be one of: pending, processing, completed, failed, cancelled');
            END
            """,
            (),
        ),
        (
            """
            CREATE TRIGGER IF NOT EXISTS check_job_status_update
            BEFORE UPDATE ON jobs
            WHEN NEW.status NOT IN ('pending', 'processing', 'completed', 'failed', 'cancelled')
            BEGIN
                SELECT RAISE(ABORT, 'Invalid status value. Must be one of: pending, processing, completed, failed, cancelled');
            END
            """,
            (),
        ),
    ]
    
    # Google integration tokens
    google_tokens_tables = [
        (
            """
            CREATE TABLE IF NOT EXISTS google_integration_tokens (
                token_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                integration TEXT NOT NULL,
                access_token TEXT,
                refresh_token TEXT,
                expiry TEXT,
                token_type TEXT,
                scopes TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, integration),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """,
            (),
        ),
    ]
    
    # Documents table (needed before document_versions)
    documents_tables = [
        (
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
                drive_folder_id TEXT,
                drive_drafts_folder_id TEXT,
                drive_media_folder_id TEXT,
                drive_published_folder_id TEXT,
                drive_file_id TEXT,
                drive_revision_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at DESC)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_ref)", ()),
    ]

    # Projects table built on top of documents
    projects_tables = [
        (
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                document_id TEXT NOT NULL UNIQUE,
                youtube_url TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                    'pending',
                    'transcript_ready',
                    'embedded',
                    'blog_generated',
                    'failed'
                )),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id, created_at DESC)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)", ()),
    ]

    # Transcript chunks linked to projects/documents
    transcript_chunks_tables = [
        (
            """
            CREATE TABLE IF NOT EXISTS transcript_chunks (
                chunk_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL,
                text_preview TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE,
                UNIQUE(project_id, chunk_index)
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_transcript_chunks_project ON transcript_chunks(project_id, chunk_index)", ()),
    ]
    
    # Document versions
    document_versions_tables = [
        (
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
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_document_versions_document ON document_versions(document_id, version DESC)", ()),
        ("CREATE UNIQUE INDEX IF NOT EXISTS unique_document_version ON document_versions(document_id, version)", ()),
    ]
    
    # Document exports
    document_exports_tables = [
        (
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
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_document_exports_document ON document_exports(document_id, created_at DESC)", ()),
    ]
    
    # Document exports triggers
    document_exports_triggers = [
        (
            """
            CREATE TRIGGER IF NOT EXISTS check_document_export_status 
            BEFORE INSERT ON document_exports
            WHEN NEW.status NOT IN ('queued','pending','processing','completed','failed','cancelled')
            BEGIN
                SELECT RAISE(ABORT, 'Invalid status value for document_exports.');
            END
            """,
            (),
        ),
        (
            """
            CREATE TRIGGER IF NOT EXISTS check_document_export_status_update
            BEFORE UPDATE ON document_exports
            WHEN NEW.status NOT IN ('queued','pending','processing','completed','failed','cancelled')
            BEGIN
                SELECT RAISE(ABORT, 'Invalid status value for document_exports.');
            END
            """,
            (),
        ),
        (
            """
            CREATE TRIGGER IF NOT EXISTS document_exports_set_updated_at
            AFTER UPDATE ON document_exports
            WHEN NEW.updated_at = OLD.updated_at
            BEGIN
                UPDATE document_exports SET updated_at = datetime('now') WHERE export_id = OLD.export_id;
            END
            """,
            (),
        ),
    ]
    
    # Pipeline events
    pipeline_events_tables = [
        (
            """
            CREATE TABLE IF NOT EXISTS pipeline_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                job_id TEXT,
                event_type TEXT NOT NULL,
                stage TEXT,
                status TEXT,
                message TEXT,
                data TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_pipeline_events_user ON pipeline_events(user_id, created_at DESC)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_pipeline_events_job ON pipeline_events(job_id, sequence DESC)", ()),
    ]
    
    # Pipeline events triggers
    pipeline_events_triggers = [
        (
            """
            CREATE TRIGGER IF NOT EXISTS check_pipeline_event_type
            BEFORE INSERT ON pipeline_events
            WHEN NEW.event_type NOT IN (
                'ingest_youtube',
                'drive_workspace',
                'drive_sync',
                'optimize_drive',
                'ingest_drive',
                'generate_blog',
                'outline.generate',
                'chapters.organize',
                'blog.compose'
            )
            BEGIN
                SELECT RAISE(ABORT, 'Invalid event_type value for pipeline_events.');
            END
            """,
            (),
        ),
        (
            """
            CREATE TRIGGER IF NOT EXISTS check_pipeline_event_type_update
            BEFORE UPDATE ON pipeline_events
            WHEN NEW.event_type NOT IN (
                'ingest_youtube',
                'drive_workspace',
                'drive_sync',
                'optimize_drive',
                'ingest_drive',
                'generate_blog',
                'outline.generate',
                'chapters.organize',
                'blog.compose'
            )
            BEGIN
                SELECT RAISE(ABORT, 'Invalid event_type value for pipeline_events.');
            END
            """,
            (),
        ),
    ]
    
    # Drive workspaces
    drive_workspaces_tables = [
        (
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
            """,
            (),
        ),
    ]
    
    # Drive watches
    drive_watches_tables = [
        (
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
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_drive_watches_user ON drive_watches(user_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_drive_watches_expires_at ON drive_watches(expires_at)", ()),
    ]
    
    # Drive watches triggers
    drive_watches_triggers = [
        (
            """
            CREATE TRIGGER IF NOT EXISTS drive_watches_set_updated_at
            AFTER UPDATE ON drive_watches
            WHEN NEW.updated_at = OLD.updated_at
            BEGIN
                UPDATE drive_watches SET updated_at = datetime('now') WHERE watch_id = OLD.watch_id;
            END
            """,
            (),
        ),
    ]
    
    # Usage events
    usage_events_tables = [
        (
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ('download','transcribe','persist','outline','chapters','compose')),
                metrics TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_usage_events_user_created ON usage_events(user_id, created_at DESC)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_usage_events_job ON usage_events(job_id, created_at DESC)", ()),
    ]
    
    # Step invocations
    step_invocations_tables = [
        (
            """
            CREATE TABLE IF NOT EXISTS step_invocations (
                idempotency_key TEXT NOT NULL,
                user_id TEXT NOT NULL,
                step_type TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_body TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (idempotency_key, user_id)
            )
            """,
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS idx_step_invocations_user ON step_invocations(user_id, created_at DESC)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_step_invocations_user_hash ON step_invocations(user_id, request_hash)", ()),
    ]
    
    # Apply schema in dependency order
    try:
        await db.batch(core_tables)
        logger.info("Applied core tables (users)")
        
        await db.batch(api_keys_tables)
        logger.info("Applied api_keys table")
        
        await db.batch(documents_tables)
        logger.info("Applied documents table")

        await db.batch(projects_tables)
        logger.info("Applied projects table")

        await db.batch(transcript_chunks_tables)
        logger.info("Applied transcript_chunks table")
        
        await db.batch(document_versions_tables)
        logger.info("Applied document_versions table")
        
        await db.batch(document_exports_tables)
        await db.batch(document_exports_triggers)
        logger.info("Applied document_exports table and triggers")
        
        await db.batch(jobs_tables)
        await db.batch(jobs_triggers)
        logger.info("Applied jobs table and triggers")
        
        await db.batch(pipeline_events_tables)
        await db.batch(pipeline_events_triggers)
        logger.info("Applied pipeline_events table and triggers")
        
        await db.batch(google_tokens_tables)
        logger.info("Applied google_integration_tokens table")
        
        await db.batch(drive_workspaces_tables)
        logger.info("Applied drive_workspaces table")
        
        await db.batch(drive_watches_tables)
        await db.batch(drive_watches_triggers)
        logger.info("Applied drive_watches table and triggers")
        
        await db.batch(usage_events_tables)
        logger.info("Applied usage_events table")
        
        await db.batch(step_invocations_tables)
        logger.info("Applied step_invocations table")
        
        logger.info("Full database schema applied successfully")
    except Exception as e:
        logger.error(f"Error applying full schema: {e}", exc_info=True)
        raise DatabaseError(f"Failed to apply database schema: {str(e)}")


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
    return _rows_to_dicts(rows)


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
    row_dicts = _rows_to_dicts(rows)
    total_events = 0
    total_bytes = 0
    total_duration = 0.0
    for r in row_dicts:
        total_events += 1
        try:
            metrics_raw = None
            metrics_raw = r.get("metrics")
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
        if isinstance(row, dict) or hasattr(row, "keys"):
            count_dict = _jsproxy_to_dict(row)
            return int(count_dict.get("cnt", 0) or 0)
        # Some drivers may return a list/iterable for single-row queries
        if isinstance(row, (list, tuple)) and row:
            first = row[0]
            if isinstance(first, dict) or hasattr(first, "keys"):
                count_dict = _jsproxy_to_dict(first)
                return int(count_dict.get("cnt", 0) or 0)
            if isinstance(first, (list, tuple)):
                return int(first[0]) if first else 0
            return int(first)
        return int(row)
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
            d = _jsproxy_to_dict(row)
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
    return _rows_to_dicts(rows)


async def mark_notification_seen(db: Database, user_id: str, notification_id: str) -> None:
    await db.execute(
        "UPDATE notification_deliveries SET seen_at = datetime('now') WHERE user_id = ? AND notification_id = ?",
        (user_id, notification_id),
    )


async def create_user_session(
    db: Database,
    session_id: str,
    user_id: str,
    expires_at: datetime,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Create or update a user session in the user_sessions table.
    
    Sessions are stored in D1 and tracked via a session_id cookie. They coexist with
    JWT tokens (stored in access_token cookie) - JWTs provide stateless authentication
    while sessions provide stateful tracking (activity, notifications, metadata).
    
    Session data is stored unencrypted in D1 (Cloudflare provides encryption at rest).
    No field-level encryption is used.
    
    Args:
        db: Database connection
        session_id: Unique session identifier (typically from cookie)
        user_id: User identifier this session belongs to
        expires_at: When the session expires (UTC datetime)
        ip_address: Optional IP address of the client
        user_agent: Optional user agent string
        extra: Optional JSON-serializable metadata dictionary
    
    The session is stored in the user_sessions table with the following structure:
    - session_id (PRIMARY KEY): Unique session identifier
    - user_id: Foreign key to users table
    - created_at: When the session was created
    - last_seen_at: Last activity timestamp (updated by touch_user_session)
    - expires_at: Session expiration time
    - last_notification_id: Cursor for notification streaming
    - ip_address, user_agent: Client metadata
    - revoked_at: Revocation timestamp (NULL if active)
    - extra: JSON metadata (e.g., OAuth provider)
    """
    now = datetime.now(timezone.utc)
    # D1 doesn't accept Python None/undefined - use empty strings for optional fields
    ip_address_val = ip_address if ip_address is not None else ""
    user_agent_val = user_agent if user_agent is not None else ""
    
    await db.execute(
        """
        INSERT INTO user_sessions (session_id, user_id, created_at, last_seen_at, expires_at, ip_address, user_agent, extra)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            user_id = excluded.user_id,
            last_seen_at = excluded.last_seen_at,
            expires_at = excluded.expires_at,
            ip_address = excluded.ip_address,
            user_agent = excluded.user_agent,
            extra = excluded.extra
        """,
        (
            session_id,
            user_id,
            _serialize_timestamp(now),
            _serialize_timestamp(now),
            _serialize_timestamp(expires_at),
            ip_address_val,
            user_agent_val,
            json.dumps(extra or {}),
        ),
    )


async def get_user_session(db: Database, session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a user session by session_id.
    
    Sessions are loaded from the user_sessions table in D1. The session_id typically
    comes from the session_id cookie set during login.
    
    Args:
        db: Database connection
        session_id: Session identifier to look up
    
    Returns:
        Dictionary with session fields (session_id, user_id, created_at, last_seen_at,
        expires_at, last_notification_id, ip_address, user_agent, revoked_at, extra)
        or None if session not found
    """
    row = await db.execute(
        "SELECT session_id, user_id, created_at, last_seen_at, expires_at, last_notification_id, ip_address, user_agent, revoked_at, extra FROM user_sessions WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return None
    # Convert JsProxy to dict
    return _jsproxy_to_dict(row)


async def touch_user_session(
    db: Database,
    session_id: str,
    *,
    last_seen_at: datetime | str | None = None,
    expires_at: datetime | str | None = None,
    last_notification_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    revoked_at: datetime | str | None = None,
) -> None:
    """Update session fields (touch/refresh session).
    
    Used to update session activity (last_seen_at), extend expiration, update
    notification cursor, or revoke the session.
    
    Args:
        db: Database connection
        session_id: Session identifier to update
        last_seen_at: Update last activity timestamp
        expires_at: Update expiration time
        last_notification_id: Update notification cursor
        extra: Update metadata dictionary
        revoked_at: Set revocation timestamp (to revoke session)
    """
    updates: list[str] = []
    params: list[Any] = []
    if last_seen_at is not None:
        updates.append("last_seen_at = ?")
        params.append(_serialize_timestamp(last_seen_at))
    if expires_at is not None:
        updates.append("expires_at = ?")
        params.append(_serialize_timestamp(expires_at))
    if last_notification_id is not None:
        updates.append("last_notification_id = ?")
        params.append(last_notification_id)
    if extra is not None:
        updates.append("extra = ?")
        params.append(json.dumps(extra))
    if revoked_at is not None:
        updates.append("revoked_at = ?")
        params.append(_serialize_timestamp(revoked_at))
    if not updates:
        return
    params.append(session_id)
    query = f"UPDATE user_sessions SET {', '.join(updates)} WHERE session_id = ?"
    await db.execute(query, tuple(params))


async def delete_user_session(db: Database, session_id: str, *, user_id: Optional[str] = None) -> None:
    """Delete a user session by session_id.
    
    If user_id is provided, validates that the session belongs to that user
    before deletion to prevent unauthorized session revocation.
    
    Uses atomic DELETE with WHERE clause to eliminate TOCTOU race conditions.
    """
    if user_id:
        # Atomic delete with ownership check - eliminates race condition by combining
        # the ownership verification and deletion into a single database operation
        result = await db.execute(
            "DELETE FROM user_sessions WHERE session_id = ? AND user_id = ?",
            (session_id, user_id),
        )
        if not result:
            logger.warning(
                "Session not found or belongs to different user",
                extra={"session_id": session_id, "user_id": user_id},
            )
    else:
        await db.execute("DELETE FROM user_sessions WHERE session_id = ?", (session_id,))


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
    if notify_level and settings.enable_notifications:
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
    for event in _rows_to_dicts(rows):
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
    

async def create_project(db: Database, user_id: str, document_id: str, youtube_url: str) -> Dict[str, Any]:
    """Insert a new project row and return it as a plain dict.

    Timestamps use SQL's datetime('now') format for consistency with the rest
    of the schema.
    """
    project_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO projects (project_id, user_id, document_id, youtube_url, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'pending', datetime('now'), datetime('now'))
        """,
        (project_id, user_id, document_id, youtube_url),
    )
    row = await db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
    return _jsproxy_to_dict(row) if row else {
        "project_id": project_id,
        "user_id": user_id,
        "document_id": document_id,
        "youtube_url": youtube_url,
        "status": "pending",
        "created_at": None,
        "updated_at": None,
    }


async def get_project(db: Database, project_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Load a project by id, scoping to the given user_id."""
    row = await db.execute(
        "SELECT * FROM projects WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    )
    return _jsproxy_to_dict(row) if row else None


async def update_project_status(db: Database, project_id: str, user_id: str, status: str) -> int:
    """Update the status and updated_at timestamp for a user's project.

    Returns the number of rows affected so callers can detect missing or
    unauthorized projects.
    """
    await db.execute(
        "UPDATE projects SET status = ?, updated_at = datetime('now') WHERE project_id = ? AND user_id = ?",
        (status, project_id, user_id),
    )
    # Fetch back the row to confirm existence/ownership.
    row = await db.execute(
        "SELECT 1 FROM projects WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    )
    return 1 if row else 0


async def create_transcript_chunk(
    db: Database,
    *,
    chunk_id: str,
    project_id: str,
    document_id: str,
    chunk_index: int,
    start_char: int,
    end_char: int,
    text_preview: str,
) -> None:
    """Insert a single transcript chunk row."""
    await db.execute(
        """
        INSERT INTO transcript_chunks
            (chunk_id, project_id, document_id, chunk_index, start_char, end_char, text_preview)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chunk_id, project_id, document_id, chunk_index, start_char, end_char, text_preview),
    )


async def list_transcript_chunks(db: Database, project_id: str, user_id: str) -> List[Dict[str, Any]]:
    """List transcript chunks for a project owned by the given user.

    Results are ordered by chunk_index and restricted via a join on projects
    so only the owner can see their project's transcript chunks.
    """
    rows = await db.execute_all(
        """
        SELECT tc.chunk_id, tc.chunk_index, tc.start_char, tc.end_char, tc.text_preview
        FROM transcript_chunks AS tc
        JOIN projects AS p ON p.project_id = tc.project_id
        WHERE tc.project_id = ? AND p.user_id = ?
        ORDER BY tc.chunk_index ASC
        """,
        (project_id, user_id),
    )
    return _rows_to_dicts(rows)


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
