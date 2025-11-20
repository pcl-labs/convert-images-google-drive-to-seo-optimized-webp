# Cloudflare Workers Python Gotchas

This document captures critical gotchas and best practices for developing Python applications in Cloudflare Workers. These are lessons learned from debugging real issues.

## Table of Contents

1. [JavaScript Interop (Pyodide)](#javascript-interop-podide)
2. [Fetch API Usage](#fetch-api-usage)
3. [Threading and Concurrency](#threading-and-concurrency)
4. [File System Access](#file-system-access)
5. [Database Access](#database-access)
6. [Environment Variables](#environment-variables)
7. [Time and Date Operations](#time-and-date-operations)
8. [Error Handling](#error-handling)
9. [ASGI Middleware and Async Operations](#asgi-middleware-and-async-operations)
10. [HTTP Cookies and Set-Cookie Headers](#http-cookies-and-set-cookie-headers)

---

## JavaScript Interop (Pyodide)

### ⚠️ **CRITICAL: Python Dicts Must Be Explicitly Converted for JavaScript APIs**

**Problem:** When passing Python dictionaries to JavaScript functions (like `fetch`), Pyodide may not automatically convert them correctly, leading to silent failures or unexpected behavior.

**Example - Fetch API:**
```python
# ❌ WRONG - May not work correctly
from js import fetch

fetch_options = {
    "method": "POST",
    "headers": {"Content-Type": "application/json"},
    "body": json_data
}
response = await fetch(url, fetch_options)  # May fail silently or behave incorrectly
```

**Solution:**
```python
# ✅ CORRECT - Explicitly convert to JavaScript object
from js import Object as JSObject

fetch_options_dict = {
    "method": "POST",
    "headers": {"Content-Type": "application/json"},
    "body": json_data
}

# Convert entire dict to JavaScript object
if JSObject is not None:
    fetch_options = JSObject.fromEntries([
        [k, v] for k, v in fetch_options_dict.items()
    ])
else:
    fetch_options = fetch_options_dict

response = await fetch(url, fetch_options)
```

**When to Use:**
- Passing options to `fetch()`
- Passing configuration to any JavaScript API
- Any Python dict that needs to be a JavaScript object

**Key Takeaway:** Always use `JSObject.fromEntries()` when passing complex Python structures to JavaScript functions.

---

## Fetch API Usage

### ⚠️ **Body Type Conversion for Form Data and JSON**

**Problem:** Cloudflare Workers Python's `fetch` API (via Pyodide) expects form-encoded and JSON bodies as **strings**, not bytes.

**Example:**
```python
# ❌ WRONG - Passing bytes directly
body = urlencode(data).encode("utf-8")  # Returns bytes
fetch_options = {"body": body}
response = await fetch(url, fetch_options)  # May fail or be misinterpreted
```

**Solution:**
```python
# ✅ CORRECT - Convert bytes to string for text-based content types
body = urlencode(data).encode("utf-8")  # Prepare as bytes first
content_type = "application/x-www-form-urlencoded"

if isinstance(body, bytes):
    if content_type in ("application/x-www-form-urlencoded", "application/json"):
        fetch_options["body"] = body.decode("utf-8")  # Convert to string
    else:
        fetch_options["body"] = body  # Keep binary data as bytes
```

**Content Types That Need String Conversion:**
- `application/x-www-form-urlencoded`
- `application/json`
- Any text-based content type

**Content Types That Should Stay Bytes:**
- `application/octet-stream`
- `image/*`, `video/*`, `audio/*`
- Any binary content type

**Key Takeaway:** Always decode bytes to string for form-encoded and JSON bodies before passing to `fetch()`.

---

### ⚠️ **Headers Must Be Converted to JavaScript Object**

**Problem:** Even when converting the main fetch options dict, nested structures like headers may not convert correctly.

**Solution:**
```python
# ✅ CORRECT - Convert headers separately
from js import Object as JSObject

request_headers = {"Content-Type": "application/json", "Authorization": "Bearer token"}

if JSObject is not None and request_headers:
    fetch_options_dict["headers"] = JSObject.fromEntries([
        [k, v] for k, v in request_headers.items()
    ])
else:
    fetch_options_dict["headers"] = request_headers
```

**Key Takeaway:** Convert nested dicts (like headers) explicitly, not just the top-level options dict.

---

## Threading and Concurrency

### ⚠️ **No Threading Support - Remove All Locks**

**Problem:** Cloudflare Workers are single-threaded. Using `threading.Lock()`, `threading.RLock()`, or any threading primitives will fail or cause issues.

**Example:**
```python
# ❌ WRONG - Will fail in Workers
import threading
_lock = threading.Lock()

def get_db():
    with _lock:  # Will fail or hang
        return database
```

**Solution:**
```python
# ✅ CORRECT - No locks needed, Workers are single-threaded
def get_db():
    return database  # Safe without locks
```

**What to Remove:**
- `threading.Lock()`
- `threading.RLock()`
- `threading.Event()`
- `threading.Condition()`
- Any `with lock:` blocks

**Key Takeaway:** Workers are single-threaded per isolate. Locks are unnecessary and will cause problems.

---

### ⚠️ **asyncio.Lock() May Not Work Correctly**

**Problem:** While `asyncio.Lock()` is technically available, it may not behave correctly in the Workers environment, especially with `time.monotonic()`.

**Example:**
```python
# ⚠️ RISKY - May cause issues
import asyncio
_lock = asyncio.Lock()

async def rate_limit():
    async with _lock:  # May not work as expected
        # Rate limiting logic using time.monotonic()
        pass
```

**Solution:**
- Use Cloudflare KV or D1 for distributed state
- Use Workers KV for rate limiting counters
- Avoid in-memory state that requires locks

**Key Takeaway:** Prefer Cloudflare-native storage (KV, D1) over in-memory state with locks.

---

## File System Access

### ⚠️ **No Runtime File System Access**

**Problem:** Workers cannot access the file system at runtime. Any `open()`, `Path.read_text()`, or file operations will fail.

**Example:**
```python
# ❌ WRONG - Will fail in Workers
def load_config():
    with open("config.json") as f:  # FileNotFoundError
        return json.load(f)
```

**Solution:**
- Load files at build/startup time (during module import)
- Use Cloudflare Assets binding for static files
- Use D1 database for configuration
- Use environment variables for settings

**Key Takeaway:** All file access must happen at build time or use Cloudflare bindings.

---

## Database Access

### ⚠️ **No SQLite Fallback - D1 Required**

**Problem:** Workers don't support file system access, so SQLite (which uses files) cannot work as a fallback.

**Example:**
```python
# ❌ WRONG - SQLite won't work in Workers
try:
    db = d1_database
except:
    db = sqlite3.connect("local.db")  # Will fail - no file system
```

**Solution:**
```python
# ✅ CORRECT - Require D1, fail gracefully if unavailable
if d1_binding is None:
    raise DatabaseError("D1 database binding required in Cloudflare Workers")
db = d1_database
```

**Key Takeaway:** Always require D1 in Workers. Handle unavailability gracefully with proper error messages.

---

### ⚠️ **Database Operations Must Be Async**

**Problem:** Blocking database operations will block the entire Worker isolate.

**Solution:**
```python
# ✅ CORRECT - Always use async
async def get_user(user_id: str):
    async with db.transaction():
        return await db.fetch_one("SELECT * FROM users WHERE id = ?", user_id)
```

**Key Takeaway:** All database operations must be async in Workers.

---

### ⚠️ **CRITICAL: D1 Query Results Are JsProxy Objects - Must Convert to Python Dicts**

**Problem:** D1 database queries return `pyodide.ffi.JsProxy` objects, not Python dictionaries. Trying to use `dict()` directly on these objects will fail with `TypeError: 'pyodide.ffi.JsProxy' object is not iterable`.

**Example:**
```python
# ❌ WRONG - Will fail with TypeError
async def get_user(db: Database, user_id: str):
    result = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return dict(result)  # TypeError: 'pyodide.ffi.JsProxy' object is not iterable
```

**Solution:**
```python
# ✅ CORRECT - Use _jsproxy_to_dict helper
from .database import _jsproxy_to_dict, _jsproxy_to_list

async def get_user(db: Database, user_id: str):
    result = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if not result:
        return None
    return _jsproxy_to_dict(result)  # Converts JsProxy to Python dict

# For multiple rows (execute_all)
async def list_users(db: Database):
    rows = await db.execute_all("SELECT * FROM users")
    if not rows:
        return []
    rows_list = _jsproxy_to_list(rows)  # Converts JsProxy array to Python list
    return [_jsproxy_to_dict(row) for row in rows_list]  # Convert each row
```

**Helper Functions:**
- `_jsproxy_to_dict(obj)` - Converts a single JsProxy object to a Python dict
- `_jsproxy_to_list(obj)` - Converts a JsProxy array/list to a Python list

**Functions That Need This Fix:**
- All functions that call `db.execute()` and return a single row
- All functions that call `db.execute_all()` and return multiple rows
- Any function that uses `dict(result)` or `dict(row)` on D1 query results
- Any function that uses `[dict(row) for row in rows]` on D1 results

**Common Patterns to Fix:**
```python
# ❌ WRONG
return dict(result) if result else None
return [dict(row) for row in rows] if rows else []

# ✅ CORRECT
if not result:
    return None
return _jsproxy_to_dict(result)

if not rows:
    return []
rows_list = _jsproxy_to_list(rows)
return [_jsproxy_to_dict(row) for row in rows_list]
```

**Key Takeaway:** Always use `_jsproxy_to_dict()` and `_jsproxy_to_list()` when working with D1 query results. Never use `dict()` directly on D1 results.

---

### ⚠️ **D1 Doesn't Accept Python None Values**

**Problem:** D1 bindings don't accept Python `None` values. Passing `None` directly will cause `D1_TYPE_ERROR: Type 'undefined' not supported for value 'undefined'`.

**Example:**
```python
# ❌ WRONG - Will fail with D1_TYPE_ERROR
await db.execute(
    "INSERT INTO users (user_id, github_id, email) VALUES (?, ?, ?)",
    (user_id, None, email)  # None causes error
)
```

**Solution:**
```python
# ✅ CORRECT - Convert None to empty string for optional fields
github_id_val = github_id if github_id is not None else ""
await db.execute(
    "INSERT INTO users (user_id, github_id, email) VALUES (?, ?, ?)",
    (user_id, github_id_val, email)
)

# In SQL, use NULLIF to convert empty strings back to NULL if needed
# INSERT INTO users (user_id, github_id, email) VALUES (?, NULLIF(?, ''), ?)
```

**When to Use:**
- Optional fields in INSERT/UPDATE statements
- Fields that can be NULL in the database schema
- Any parameter that might be `None`

**Key Takeaway:** Convert `None` to empty string (`""`) before passing to D1, or use SQL `NULLIF()` to handle empty strings as NULL.

---


---

## Environment Variables

### ⚠️ **Environment Variables Must Be Injected via Runtime**

**Problem:** `os.environ` is not automatically populated in Workers. Variables must be injected via `wrangler.toml` and the runtime.

**Solution:**
```python
# ✅ CORRECT - Use runtime.apply_worker_env()
from workers.runtime import apply_worker_env

# Call before accessing os.environ
apply_worker_env(env)  # env is the Workers env object

# Now os.environ is populated
import os
api_key = os.environ.get("API_KEY")
```

**Key Takeaway:** Always call `apply_worker_env()` before accessing environment variables.

---

## Time and Date Operations

### ⚠️ **time.monotonic() May Not Work Correctly**

**Problem:** `time.monotonic()` may not behave as expected in Workers, especially when used with `asyncio.Lock()`.

**Example:**
```python
# ⚠️ RISKY - May cause issues
import time
start = time.monotonic()  # May not work correctly
```

**Solution:**
- Use `datetime.now(timezone.utc)` for timestamps
- Use D1 or KV for time-based operations
- Avoid `time.monotonic()` for rate limiting

**Key Takeaway:** Prefer `datetime` over `time.monotonic()` in Workers.

---

## Error Handling

### ⚠️ **Graceful Degradation for Optional Features**

**Problem:** Features that depend on unavailable services (like D1) should fail gracefully, not crash the entire request.

**Example:**
```python
# ❌ WRONG - Crashes entire request
def get_user_from_db():
    db = ensure_db()  # Raises HTTPException(500)
    return db.fetch_user()  # Never reached if DB unavailable
```

**Solution:**
```python
# ✅ CORRECT - Graceful degradation
def get_user_from_db():
    try:
        db = ensure_db()
        return db.fetch_user()
    except DatabaseError:
        # Return None or use JWT claims only
        return None
```

**Key Takeaway:** Always catch and handle database/service unavailability gracefully.

---

## ASGI Middleware and Async Operations

### ⚠️ **CRITICAL: Async DB Operations in Middleware Can Cause ASGI InvalidStateError**

**Problem:** Even async database operations performed BEFORE `call_next(request)` in middleware can cause `asyncio.exceptions.InvalidStateError: invalid state` errors in Cloudflare Workers Python's ASGI adapter, particularly on API endpoints that return JSON responses.

**Example:**
```python
# ⚠️ RISKY - May cause InvalidStateError on API endpoints
class FlashMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        if session and session_id:
            # Async DB write before call_next
            db = ensure_db()
            await touch_user_session(db, session_id, extra=extra_dict)  # May cause ASGI error
        
        response = await call_next(request)
        return response
```

**Error:**
```
asyncio.exceptions.InvalidStateError: invalid state
File "/lib/python3.12/site-packages/asgi.py", line 193, in send
    result.set_result(resp)
```

**Solution 1: Skip Middleware for API Endpoints**
```python
# ✅ CORRECT - Skip middleware processing for API endpoints
class FlashMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path
        if path.startswith("/api/"):
            # API endpoints return JSON, don't need flash messages
            response = await call_next(request)
            return response
        
        # Process flash messages for HTML endpoints only
        # ... rest of middleware logic
```

**Solution 2: Move Async Operations to After Response (If Possible)**
```python
# ✅ CORRECT - Do async work after response is generated (if acceptable)
class FlashMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        # Read flash messages (synchronous)
        request.state.flash_messages = flash_queue
        
        response = await call_next(request)
        
        # Do async DB write AFTER response (if acceptable for your use case)
        # Note: This may cause stale data on next request, but avoids ASGI errors
        if needs_flash_clear:
            await touch_user_session(db, session_id, extra=extra_dict)
        
        return response
```

**When This Occurs:**
- Middleware that performs async DB operations (D1 queries, updates)
- API endpoints (`/api/*`) that return JSON responses
- Static file requests (`/static/*`)
- The error occurs during response body send, not during the async operation itself

**Why It Happens:**
Cloudflare Workers Python's ASGI adapter has strict lifecycle management. Async operations in middleware, even before `call_next`, can interfere with the ASGI Future state when the response is being sent, particularly for JSON responses and static files.

**Key Takeaway:** 
- Skip middleware processing for API endpoints if they don't need the middleware functionality
- If async DB operations are required, consider moving them to after `call_next` (if acceptable)
- Test middleware thoroughly on both HTML and JSON endpoints
- SessionMiddleware works because it uses a different pattern - test your middleware carefully

---

## HTTP Cookies and Set-Cookie Headers

### ⚠️ **CRITICAL: Only One Set-Cookie Header Per Response**

**Problem:** Cloudflare Workers only sends **one** `Set-Cookie` header per HTTP response, even if you call `response.delete_cookie()` or `response.headers.append("Set-Cookie", ...)` multiple times. Only the **last** `Set-Cookie` header will be sent to the client.

**Example:**
```python
# ❌ WRONG - Only the last cookie deletion will be sent
response.delete_cookie("csrf_token", path="/", secure=is_secure)
response.delete_cookie("access_token", path="/", secure=is_secure)  # Only this one is sent!
```

**Impact:**
- When logging out or deleting accounts, you may need to clear multiple cookies (`access_token`, `csrf_token`, session cookies, OAuth state cookies, etc.)
- If you delete cookies in the wrong order, critical cookies like `access_token` may not be cleared, leaving users authenticated

**Solution:**
```python
# ✅ CORRECT - Delete the most important cookie LAST
# Delete less critical cookies first (won't be sent, but keeps code clean)
response.delete_cookie(COOKIE_OAUTH_STATE, path="/", samesite="lax", httponly=True, secure=is_secure)
response.delete_cookie("csrf_token", path="/", samesite="lax", httponly=True, secure=is_secure)
response.delete_cookie("google_redirect_uri", path="/", samesite="lax", httponly=True, secure=is_secure)
# ... other cookies ...

# Delete access_token LAST - this is the Set-Cookie header that will be sent
# (most important for logout, so it takes priority)
response.delete_cookie("access_token", path="/", samesite="lax", httponly=True, secure=is_secure)
```

**Best Practices:**
1. **Prioritize critical cookies:** Delete the most important cookie (usually `access_token` for authentication) **last** so it's the one that gets sent
2. **Order matters:** The last `delete_cookie()` call determines which cookie deletion is sent
3. **Test cookie deletion:** Always verify that critical cookies are actually cleared in the browser/HTTP response
4. **Document the limitation:** If you need to clear multiple cookies, document which one takes priority

**When This Matters:**
- Logout endpoints (`POST /auth/logout`)
- Account deletion endpoints (`POST /dashboard/account/delete`)
- Session expiration/cleanup
- Any endpoint that needs to clear multiple authentication cookies

**Key Takeaway:** Cloudflare Workers only sends one `Set-Cookie` header per response. Always delete the most critical cookie (like `access_token`) **last** to ensure it's the one that gets sent to the client.

---

## Summary Checklist

When developing for Cloudflare Workers Python:

- [ ] **JavaScript Interop:** Use `JSObject.fromEntries()` for Python dicts passed to JS functions
- [ ] **Fetch API:** Convert form/JSON bodies from bytes to string
- [ ] **Fetch Headers:** Convert headers dict explicitly with `JSObject.fromEntries()`
- [ ] **Threading:** Remove all `threading.Lock()` and related primitives
- [ ] **Async Locks:** Avoid `asyncio.Lock()` with `time.monotonic()`
- [ ] **File System:** No runtime file access - use bindings or load at startup
- [ ] **Database:** Require D1, no SQLite fallback
- [ ] **Database Ops:** Always use async database operations
- [ ] **D1 JsProxy Conversion:** Always use `_jsproxy_to_dict()` and `_jsproxy_to_list()` for D1 query results
- [ ] **D1 None Values:** Convert Python `None` to empty string (`""`) before passing to D1
- [ ] **Environment:** Call `apply_worker_env()` before accessing `os.environ`
- [ ] **Time:** Prefer `datetime` over `time.monotonic()`
- [ ] **Errors:** Handle service unavailability gracefully
- [ ] **ASGI Middleware:** Skip async DB operations for API endpoints, or move them after `call_next` if acceptable
- [ ] **Set-Cookie Headers:** Only one `Set-Cookie` header per response - delete critical cookies (like `access_token`) **last**

---

## Related Documentation

- [Cloudflare Workers Python Documentation](https://developers.cloudflare.com/workers/languages/python/)
- [Pyodide Documentation](https://pyodide.org/)
- [D1 Database Documentation](https://developers.cloudflare.com/d1/)

