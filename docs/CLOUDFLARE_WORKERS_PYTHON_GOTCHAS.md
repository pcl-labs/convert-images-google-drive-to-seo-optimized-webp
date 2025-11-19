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
- [ ] **Environment:** Call `apply_worker_env()` before accessing `os.environ`
- [ ] **Time:** Prefer `datetime` over `time.monotonic()`
- [ ] **Errors:** Handle service unavailability gracefully

---

## Related Documentation

- [Cloudflare Workers Python Documentation](https://developers.cloudflare.com/workers/languages/python/)
- [Pyodide Documentation](https://pyodide.org/)
- [D1 Database Documentation](https://developers.cloudflare.com/d1/)

