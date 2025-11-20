# Disabled Features to Achieve Green State

This document tracks features that have been temporarily disabled to get the application working in Cloudflare Workers without errors.

## Status: ✅ Green State Achieved

- `/auth/me` - Returns 200 with real user data
- `/health` - Returns 200
- `/test/fetch` - Returns 200 (fetch API working)
- `/dashboard` - Returns 200 with full dashboard (auth working, DB calls enabled)
- Live Updates/Notifications - ✅ Working via HTTP polling (migrated from SSE)
- Sessions - ✅ Enabled and working
- FlashMiddleware - ✅ Enabled and working (fixed ASGI error)

## Disabled Middleware

### 1. FlashMiddleware
**Status:** ✅ **ENABLED** (Fixed)  
**Location:** `src/workers/api/app_factory.py:273`  
**Reason:** Flash messages for toast notifications.  
**Impact:** Flash messages work correctly.  

**Fix Applied:**
- **Root Cause**: Doing async DB write (`touch_user_session`) AFTER `call_next(request)` (after response generated) interfered with Cloudflare Workers Python ASGI response lifecycle
- **Solution**: Moved DB write to BEFORE `call_next(request)`, matching SessionMiddleware pattern
- **Key Insight**: In Cloudflare Workers Python ASGI adapter, avoid async operations after response is generated but before returning it, as this causes `asyncio.exceptions.InvalidStateError` when ASGI Future tries to set result during response body send

**Can be re-enabled:** ✅ **ENABLED** - Working correctly after fix

### 2. SecurityHeadersMiddleware
**Status:** Disabled  
**Location:** `src/workers/api/app_factory.py` (not currently registered)  
**Reason:** Testing minimal middleware stack first.  
**Impact:** No security headers (X-Frame-Options, X-Content-Type-Options, etc.).  
**Can be re-enabled:** ✅ Yes, should be safe to add back (no DB needed, low risk).

### 3. RateLimitMiddleware
**Status:** Disabled  
**Location:** `src/workers/api/app_factory.py:275-281`  
**Reason:** Uses `time.monotonic()` and `asyncio.Lock()` which may not work correctly in Cloudflare Workers.  
**Impact:** No rate limiting protection.  
**Can be re-enabled:** Needs testing - may need to use Cloudflare KV or Workers KV instead of in-memory tracking.

## Enabled Middleware

- ✅ `AuthCookieMiddleware` - Reads JWT from cookies/headers, sets `request.state.user`
- ✅ `SessionMiddleware` - Manages browser sessions in D1, used for OAuth flows and activity tracking
- ✅ `FlashMiddleware` - Flash messages for toast notifications (fixed ASGI error)
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

### Frontend: SSE to HTTP Polling Migration
**Files Modified:**
- `src/workers/templates/base.html` - Replaced `EventSource('/api/stream')` SSE implementation with HTTP polling to `/api/notifications`
- `src/workers/templates/components/overlays/toast.html` - Fixed Alpine.js regex error (removed extra backslashes)

**Reason:** SSE (Server-Sent Events) doesn't work in Cloudflare Workers Python - requires special `ctx` parameter that's not available. HTTP polling works reliably and provides same functionality.

## Next Steps to Re-enable Features

1. **SecurityHeadersMiddleware** - Add back (no DB needed, low risk)
2. **FlashMiddleware** - ✅ **FIXED AND ENABLED** - Root cause identified and resolved
3. **RateLimitMiddleware** - Needs alternative implementation using KV or remove entirely


## Notes

- ✅ D1 database is configured in `wrangler.toml` (binding: "DB", database_id: "933d76cf-a988-4a71-acc6-d884278c6402")
- ✅ Auth works with JWT tokens and sessions
- ✅ Dashboard endpoint fully functional with all features
- ✅ Live Updates/Notifications working via HTTP polling (migrated from SSE due to Cloudflare Workers Python limitations)
- ✅ Sessions enabled and working for OAuth flows and activity tracking
- ✅ FlashMiddleware enabled and working (fixed ASGI InvalidStateError by moving DB write before call_next)
- ✅ Frontend migrated from SSE (EventSource) to HTTP polling for notifications
- ✅ FlashMiddleware enabled and working (fixed ASGI InvalidStateError by moving DB write before call_next)
- ✅ Frontend migrated from SSE (EventSource) to HTTP polling for notifications

