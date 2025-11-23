-- Database schema for Cloudflare D1

-- Users table (with NOT NULL and UNIQUE constraints on email)
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    github_id TEXT UNIQUE,
    google_id TEXT UNIQUE,
    email TEXT NOT NULL UNIQUE,
    preferences TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Migration: Enforce NOT NULL and UNIQUE constraints on users.email
-- These steps handle existing data in tables that were created before constraints were added
-- They are safe to run multiple times (idempotent) and will only affect existing data

-- Destructive cleanup block: these deletes are executed as part of the migration
-- Remote D1 manages the overall migration transaction automatically.

-- Step 1: Handle NULL emails (delete rows with NULL email as they're invalid)
DELETE FROM users WHERE email IS NULL;

-- Step 2: Handle duplicate emails without ROW_NUMBER (D1/SQLite-compatible)
-- Keep the canonical (oldest) row per email based on created_at, then user_id as tiebreaker.
-- Delete any other rows for the same email.
DELETE FROM users
WHERE email IS NOT NULL
  AND user_id != (
    SELECT u.user_id
    FROM users u
    WHERE u.email = users.email
    ORDER BY u.created_at ASC, u.user_id ASC
    LIMIT 1
  );

-- Step 3: Create UNIQUE index on email (if it doesn't exist)
-- This provides an additional enforcement layer and improves query performance
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- Browser session tracking
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
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id, created_at DESC);

-- API Keys table
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used TEXT,
    salt TEXT, -- Base64-encoded salt for PBKDF2 (NULL for legacy SHA256 keys)
    iterations INTEGER, -- PBKDF2 iteration count (NULL for legacy SHA256 keys)
    lookup_hash TEXT, -- Short SHA-256 hex prefix for targeted lookups (NULL for legacy rows)
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Migration: Add salt and iterations columns for PBKDF2 support
-- These columns are nullable to support legacy SHA256-hashed keys
-- Existing keys will have NULL values and will be migrated on next use
-- Note: If columns already exist, these statements will fail - that's okay
-- For existing databases, run these manually or use a migration script with error handling
-- ALTER TABLE api_keys ADD COLUMN salt TEXT;
-- ALTER TABLE api_keys ADD COLUMN iterations INTEGER;

-- Jobs table
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
    progress TEXT NOT NULL DEFAULT '{}', -- JSON string
    drive_folder TEXT,
    extensions TEXT, -- JSON array string
    job_type TEXT NOT NULL DEFAULT 'optimize_drive',
    document_id TEXT,
    output TEXT,
    payload TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);

-- Migration: Add CHECK constraint to jobs.status for existing databases
-- This enforces the status state machine without data loss
-- Note: This will fail if any existing rows have invalid status values
-- Run data cleanup first if needed: UPDATE jobs SET status = 'pending' WHERE status NOT IN ('pending', 'processing', 'completed', 'failed', 'cancelled');
-- For SQLite/D1, we need to recreate the table or use a workaround since ALTER TABLE ADD CONSTRAINT is not directly supported
-- Alternative approach: Create a trigger to enforce the constraint
CREATE TRIGGER IF NOT EXISTS check_job_status 
BEFORE INSERT ON jobs
WHEN NEW.status NOT IN ('pending', 'processing', 'completed', 'failed', 'cancelled')
BEGIN
    SELECT RAISE(ABORT, 'Invalid status value. Must be one of: pending, processing, completed, failed, cancelled');
END;

CREATE TRIGGER IF NOT EXISTS check_job_status_update
BEFORE UPDATE ON jobs
WHEN NEW.status NOT IN ('pending', 'processing', 'completed', 'failed', 'cancelled')
BEGIN
    SELECT RAISE(ABORT, 'Invalid status value. Must be one of: pending, processing, completed, failed, cancelled');
END;

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_session_id ON jobs(session_id);
-- Optimize retry scheduling lookups
CREATE INDEX IF NOT EXISTS idx_jobs_status_next_attempt ON jobs(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_lookup_hash ON api_keys(lookup_hash);
CREATE INDEX IF NOT EXISTS idx_users_github_id ON users(github_id);
CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id);

-- Google OAuth tokens per integration (Drive, YouTube, etc.)
DROP TABLE IF EXISTS google_tokens;
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
);

-- Phase 1: Content normalization and unified job types
-- Create documents table
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
);

CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_ref);

-- Document versions table for immutable snapshots
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
);

CREATE INDEX IF NOT EXISTS idx_document_versions_document ON document_versions(document_id, version DESC);
-- Ensure uniqueness for document versions across existing databases
CREATE UNIQUE INDEX IF NOT EXISTS unique_document_version ON document_versions(document_id, version);

-- Document export requests (preparing for connectors)
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
);

CREATE INDEX IF NOT EXISTS idx_document_exports_document ON document_exports(document_id, created_at DESC);

-- Pipeline events for streaming ingest status
CREATE TABLE IF NOT EXISTS pipeline_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    job_id TEXT,
    session_id TEXT,
    event_type TEXT NOT NULL,
    stage TEXT,
    status TEXT,
    message TEXT,
    data TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pipeline_events_user ON pipeline_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_job ON pipeline_events(job_id, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_session ON pipeline_events(session_id, sequence DESC);

-- Enforce allowed values for pipeline_events.event_type for existing databases
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
END;

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
END;

-- Constraints and triggers for document_exports status and timestamp maintenance
CREATE TRIGGER IF NOT EXISTS check_document_export_status 
BEFORE INSERT ON document_exports
WHEN NEW.status NOT IN ('queued','pending','processing','completed','failed','cancelled')
BEGIN
    SELECT RAISE(ABORT, 'Invalid status value for document_exports.');
END;

CREATE TRIGGER IF NOT EXISTS check_document_export_status_update
BEFORE UPDATE ON document_exports
WHEN NEW.status NOT IN ('queued','pending','processing','completed','failed','cancelled')
BEGIN
    SELECT RAISE(ABORT, 'Invalid status value for document_exports.');
END;

-- Maintain updated_at on updates
CREATE TRIGGER IF NOT EXISTS document_exports_set_updated_at
AFTER UPDATE ON document_exports
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE document_exports SET updated_at = datetime('now') WHERE export_id = OLD.export_id;
    END;

-- Drive workspace metadata per user
CREATE TABLE IF NOT EXISTS drive_workspaces (
    user_id TEXT PRIMARY KEY,
    root_folder_id TEXT NOT NULL,
    drafts_folder_id TEXT NOT NULL,
    published_folder_id TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Active Drive change notification watches per document
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
);

CREATE INDEX IF NOT EXISTS idx_drive_watches_user ON drive_watches(user_id);
CREATE INDEX IF NOT EXISTS idx_drive_watches_expires_at ON drive_watches(expires_at);

CREATE TRIGGER IF NOT EXISTS drive_watches_set_updated_at
AFTER UPDATE ON drive_watches
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE drive_watches SET updated_at = datetime('now') WHERE watch_id = OLD.watch_id;
END;

-- Enforce that documents.latest_version_id references an existing document_versions.version_id
-- Rely on FOREIGN KEY (latest_version_id) REFERENCES document_versions(version_id) ON DELETE SET NULL
-- No additional triggers needed here.

-- Phase 2: Usage metering
CREATE TABLE IF NOT EXISTS usage_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('download','transcribe','persist','outline','chapters','compose')),
    metrics TEXT, -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_usage_events_user_created ON usage_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_job ON usage_events(job_id, created_at DESC);

-- Idempotent step invocations (Phase 2.5)
CREATE TABLE IF NOT EXISTS step_invocations (
    idempotency_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    step_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_body TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (idempotency_key, user_id)
);

CREATE INDEX IF NOT EXISTS idx_step_invocations_user ON step_invocations(user_id, created_at DESC);
-- Support duplicate detection and fast lookup by request hash per user
CREATE INDEX IF NOT EXISTS idx_step_invocations_user_hash ON step_invocations(user_id, request_hash);

-- Retention plan: step_invocations are ephemeral and should not be retained long-term.
-- Operational guidance:
-- - Set up a scheduled cleanup (cron/worker) to delete rows older than 24–48 hours.
--   Example (SQLite/D1): DELETE FROM step_invocations WHERE created_at < datetime('now','-48 hours');
-- - Service layer should sanitize response_body to avoid storing PII/secrets.
--   Only non-sensitive metadata should be stored. Keys such as email, tokens, passwords,
--   api_key, authorization, secret, client_secret must be redacted or omitted.

-- Projects table for YouTube-to-blog workflow
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    document_id TEXT NOT NULL UNIQUE,
    youtube_url TEXT,
    title TEXT,
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
);

CREATE INDEX IF NOT EXISTS idx_projects_user
  ON projects(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_projects_status
  ON projects(status);

-- Transcript chunks linked to projects/documents
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
);

CREATE INDEX IF NOT EXISTS idx_transcript_chunks_project
  ON transcript_chunks(project_id, chunk_index);
