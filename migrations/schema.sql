-- Database schema for Cloudflare D1

-- Users table (with NOT NULL and UNIQUE constraints on email)
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    github_id TEXT UNIQUE,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Migration: Enforce NOT NULL and UNIQUE constraints on users.email
-- These steps handle existing data in tables that were created before constraints were added
-- They are safe to run multiple times (idempotent) and will only affect existing data

-- Destructive cleanup block: wraps deletes in a transaction for atomicity.
-- WARNING: The following statements permanently remove data.
-- Consider taking a backup or migrating to a soft-delete/archive strategy before running.
BEGIN TRANSACTION;

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

COMMIT;

-- Step 3: Create UNIQUE index on email (if it doesn't exist)
-- This provides an additional enforcement layer and improves query performance
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);

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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    error TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
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
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_lookup_hash ON api_keys(lookup_hash);
CREATE INDEX IF NOT EXISTS idx_users_github_id ON users(github_id);

-- Google OAuth tokens per user
CREATE TABLE IF NOT EXISTS google_tokens (
    user_id TEXT PRIMARY KEY,
    access_token TEXT,
    refresh_token TEXT,
    expiry TEXT,
    token_type TEXT,
    scopes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_google_tokens_user_id ON google_tokens(user_id);

-- Phase 1: Content normalization and unified job types
-- Create documents table
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT,
    raw_text TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_ref);
