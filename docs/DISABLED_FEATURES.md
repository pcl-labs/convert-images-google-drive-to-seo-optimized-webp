# Disabled Features to Achieve Green State

This document tracks features that have been temporarily disabled to get the application working in Cloudflare Workers without errors.

## Status: ✅ Green State Achieved

- `/auth/me` - Returns 200 with real user data
- `/health` - Returns 200
- `/test/fetch` - Returns 200 (fetch API working)
- `/dashboard` - Returns 200 with hello world (auth working, DB calls disabled)

## Disabled Middleware

### 1. SessionMiddleware
**Status:** Disabled  
**Location:** `src/workers/api/app_factory.py:267`  
**Reason:** Requires D1 database to load sessions. Only used for toast notifications.  
**Impact:** No session management, no toast notifications via flash messages.  
**Can be re-enabled:** Yes, once D1 is properly configured and tested.

### 2. FlashMiddleware
**Status:** Disabled  
**Location:** `src/workers/api/app_factory.py:268`  
**Reason:** Depends on SessionMiddleware. Only used for toast notifications.  
**Impact:** No flash messages/toast notifications.  
**Can be re-enabled:** Yes, after SessionMiddleware is working.

### 3. SecurityHeadersMiddleware
**Status:** Disabled  
**Location:** `src/workers/api/app_factory.py:265`  
**Reason:** Testing minimal middleware stack first.  
**Impact:** No security headers (X-Frame-Options, X-Content-Type-Options, etc.).  
**Can be re-enabled:** Yes, should be safe to add back (no DB needed).

### 4. RateLimitMiddleware
**Status:** Disabled  
**Location:** `src/workers/api/app_factory.py:269-273`  
**Reason:** Uses `time.monotonic()` and `asyncio.Lock()` which may not work correctly in Cloudflare Workers.  
**Impact:** No rate limiting protection.  
**Can be re-enabled:** Needs testing - may need to use Cloudflare KV or Workers KV instead of in-memory tracking.

## Enabled Middleware

- ✅ `AuthCookieMiddleware` - Reads JWT from cookies/headers, sets `request.state.user`
- ✅ `CORSMiddleware` - CORS handling
- ✅ `RequestIDMiddleware` - Adds request ID to responses

## Code Changes

### Removed Threading Locks
**Files Modified:**
- `src/workers/api/deps.py` - Removed `_db_lock`, `_queue_lock`, `_services_lock`
- `src/workers/runtime.py` - Removed `_ENV_LOCK`
- `src/workers/api/config.py` - Removed `_settings_lock`
- `src/workers/core/drive_utils.py` - Removed `_token_lock`

**Reason:** Cloudflare Workers don't support threading. Each isolate is single-threaded, so locks are unnecessary.

### Removed SQLite Fallback
**File:** `src/workers/api/database.py`  
**Change:** `Database()` now requires D1 binding, no SQLite fallback.  
**Reason:** Workers don't support file system access. D1 is required.

### Middleware Exception Handling
**Files Modified:**
- `src/workers/api/middleware.py` - All middleware now catch exceptions gracefully
  - `SessionMiddleware` - Treats DB failure as no session
  - `FlashMiddleware` - Skips flash clear on DB failure
  - `AuthCookieMiddleware` - Continues with JWT claims only if DB unavailable

## Next Steps to Re-enable Features

1. **SecurityHeadersMiddleware** - Add back first (no DB needed, low risk)
2. **SessionMiddleware** - Requires D1 to be working, test session creation/retrieval
3. **FlashMiddleware** - Add back after SessionMiddleware works
4. **RateLimitMiddleware** - Needs alternative implementation using KV or remove entirely

## Disabled Endpoint Features

### Dashboard Endpoint
**File:** `src/workers/api/web.py:1057`  
**Status:** Simplified to hello world  
**Changes:**
- Disabled `Depends(get_current_user)` dependency - reads `request.state.user` directly instead
- Commented out all DB calls (`ensure_db()`, `list_jobs()`, `get_job_stats()`, etc.)
- Commented out sidebar template include in `base.html`
- Returns simple PlainTextResponse instead of rendering template

**Reason:** `Depends(get_current_user)` was causing 500 errors. Reading from `request.state.user` directly works.

### OAuth Callbacks
**File:** `src/workers/api/public.py:499, 561`  
**Status:** Session cookie creation disabled  
**Changes:**
- Commented out `await _issue_session_cookie()` calls in GitHub and Google OAuth callbacks
- Sessions are disabled, so session cookie creation is not needed

**Reason:** Sessions are disabled, so we don't need to create session cookies during OAuth flow.

### Form Data in Fetch
**File:** `src/workers/api/simple_http.py:171-177`  
**Status:** Fixed - form data now passed as string  
**Changes:**
- Form data (application/x-www-form-urlencoded) is now decoded to string before passing to fetch
- JSON and binary data remain as bytes

**Reason:** Cloudflare Workers `fetch` API expects form data as a string, not bytes. This was causing OAuth token exchange to fail.

### OAuth Authentication (DB Bypass)
**File:** `src/workers/api/public.py:491-504, 567-607`  
**Status:** Simplified to skip DB entirely  
**Changes:**
- GitHub OAuth callback: Removed all DB calls (`get_user_by_github_id`, `create_user`, etc.)
- Google OAuth callback: Removed all DB calls (`get_user_by_google_id`, `create_user`, etc.)
- Both callbacks now generate JWT directly from OAuth provider data
- Removed `ensure_db()` checks - no DB needed for authentication

**Reason:** DB operations were failing and blocking OAuth redirects. Authentication now works purely from OAuth provider data without any database persistence.

## Notes

- ✅ D1 database is configured in `wrangler.toml` (binding: "DB", database_id: "933d76cf-a988-4a71-acc6-d884278c6402")
- Auth works without DB if JWT token contains email (which it does)
- ✅ Dashboard endpoint works with simplified hello world response
- ⚠️ `Depends(get_current_user)` dependency causes 500 errors - needs investigation

