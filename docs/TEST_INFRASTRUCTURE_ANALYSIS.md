# Test Infrastructure Analysis: Database Backend & Isolation Issues

**Document Purpose**: Analyze test infrastructure to understand database usage, isolation problems, and root causes of UNIQUE constraint failures.

**Status**: Analysis Only - No Code Changes

---

## Table of Contents

1. [Test Harness Overview](#1-test-harness-overview)
2. [Database Usage in Tests](#2-database-usage-in-tests)
3. [Which Backend Do Tests Actually Hit?](#3-which-backend-do-tests-actually-hit)
4. [UNIQUE Constraint Root Cause](#4-unique-constraint-root-cause)
5. [Isolation Strategy Options (Analysis Only)](#5-isolation-strategy-options-analysis-only)
6. [Worker/D1 Integration Tests (If Any)](#6-workerd1-integration-tests-if-any)
7. [Summary & Recommendation](#7-summary--recommendation)

---

## 1. Test Harness Overview

### 1.1 Test Runner Configuration

**Test Runner**: `pytest` (configured in `pyproject.toml`)

**Configuration Files**:
- `pyproject.toml` (lines 20-23): Minimal pytest configuration with custom markers
- `tests/conftest.py`: Global test setup (environment variables, Python path)

**Key Configuration**:
```python
# tests/conftest.py
- Sets PYTHONPATH to include src/workers (enables absolute imports)
- Disables .env file reading (PYTEST_DISABLE_DOTENV=1)
- Sets test JWT secret if not present
- No database fixtures or teardown
```

### 1.2 Application Instantiation

**How Tests Create the App**:
- All tests use `from src.workers.api.main import app`
- Tests use FastAPI's `TestClient(app)` for HTTP requests
- No mocking of Cloudflare Workers runtime or bindings

**Example Pattern** (from `test_auth_routes.py`):
```python
@pytest.fixture
def client():
    from src.workers.api.main import app
    return TestClient(app)
```

### 1.3 Database Instantiation

**How Tests Create Database Connections**:

**Pattern 1: Direct `Database()` instantiation (most common)**
```python
db = Database()  # No arguments
# Falls back to SQLite at data/dev.db
```

**Pattern 2: Using `LOCAL_SQLITE_PATH` environment variable (isolated tests)**
```python
# tests/api/test_sessions.py (line 137)
os.environ["LOCAL_SQLITE_PATH"] = str(db_path)
db = Database()  # Uses temp file instead of data/dev.db
```

**Pattern 3: Using `tmp_path` fixture with monkeypatch (isolated tests)**
```python
# tests/api/test_pipeline_events.py (line 17)
monkeypatch.setenv("LOCAL_SQLITE_PATH", str(db_path))
db = Database()
```

**Global Database Instance**:
- Tests call `set_db_instance(db)` to set global `_db_instance` in `deps.py`
- This allows `ensure_db()` to return the test database
- **No cleanup**: Global instance persists across tests

### 1.4 Database Reset/Cleanup

**Current State**: **No automatic cleanup between tests**

**Manual Cleanup** (only in some tests):
- `test_youtube_ingest.py`: Explicit `DELETE FROM users WHERE user_id = ?` in teardown (lines 136-139, 222-225, etc.)
- `test_generate_blog_pipeline.py`: Explicit `DELETE FROM users WHERE user_id = ?` in teardown (lines 124-128)
- Most other tests: **No cleanup at all**

**Isolation Mechanisms**:
- **None** for most tests (share `data/dev.db`)
- **Temp file per test** for `test_sessions.py` (uses `tmp_path` fixture)
- **Temp file per test** for `test_pipeline_events.py` (uses `tmp_path` fixture)

---

## 2. Database Usage in Tests

### 2.1 Per-Module Summary

#### `tests/api/test_ingestion_auth.py`
- **Database Creation**: `Database()` → SQLite at `data/dev.db`
- **Global Instance**: Calls `set_db_instance(db)` once per fixture
- **User Creation**: `create_user(db, user_id=user_id, github_id=None, email=email)` (line 24)
- **Cleanup**: **None** - no teardown
- **Isolation**: **None** - shares `data/dev.db` with all other tests

#### `tests/api/test_auth_routes.py`
- **Database Creation**: **None** - tests mock `ensure_db()` to raise exceptions
- **Global Instance**: **Not set** - tests patch `deps.ensure_db` to simulate DB failures
- **User Creation**: **None** - tests don't create users
- **Cleanup**: **None**
- **Isolation**: **N/A** - no database operations

#### `tests/api/test_logout_and_delete_account.py`
- **Database Creation**: `Database()` → SQLite at `data/dev.db` (line 33)
- **Global Instance**: Calls `set_db_instance(db)` once per fixture (line 51)
- **User Creation**: `create_user(db, user_id=user_id, github_id=github_id, google_id=None, email=email)` (line 36)
  - **Fixed**: Uses unique `github_id` to avoid UNIQUE constraint violations
- **Cleanup**: **None** - no teardown
- **Isolation**: **None** - shares `data/dev.db` with all other tests

#### `tests/api/test_youtube_ingest.py`
- **Database Creation**: `Database()` → SQLite at `data/dev.db` (multiple tests, lines 74, 146, 249, etc.)
- **Global Instance**: **Not set** - each test creates its own `Database()` instance
- **User Creation**: `create_user(db, user_id=user_id, github_id=None, email=f"{user_id}@example.com")` (lines 76, 148, 254, etc.)
- **Cleanup**: **Manual** - explicit `DELETE FROM users WHERE user_id = ?` in teardown (lines 136-139, 222-225, etc.)
- **Isolation**: **Partial** - manual cleanup per test, but shares `data/dev.db`

#### `tests/api/test_sessions.py`
- **Database Creation**: Uses `isolated_db` fixture with `tmp_path` → **Temp file per test** (line 132-150)
- **Global Instance**: **Not set**
- **User Creation**: `create_user(db, user_id, email="session@example.com")` (line 158)
- **Cleanup**: **Automatic** - temp file deleted after test (line 148)
- **Isolation**: **Full** - each test gets its own SQLite file

#### `tests/api/test_pipeline_events.py`
- **Database Creation**: Uses `tmp_path` with `LOCAL_SQLITE_PATH` → **Temp file per test** (line 17)
- **Global Instance**: **Not set**
- **User Creation**: Direct SQL `INSERT OR IGNORE INTO users` (line 21)
- **Cleanup**: **Automatic** - temp file cleaned up by pytest
- **Isolation**: **Full** - each test gets its own SQLite file

#### `tests/api/test_drive_ingest.py`
- **Database Creation**: `Database()` → SQLite at `data/dev.db` (lines 85, 103, 146, 193)
- **Global Instance**: **Not set**
- **User Creation**: `create_user(db, user_id=user_id, github_id=None, email=f"{user_id}@example.com")` (lines 87, 105, 148, 195)
- **Cleanup**: **None**
- **Isolation**: **None** - shares `data/dev.db` with all other tests

#### `tests/api/test_generate_blog_pipeline.py`
- **Database Creation**: `Database()` → SQLite at `data/dev.db` (line 20)
- **Global Instance**: **Not set**
- **User Creation**: `create_user(db, user_id, email="pipeline@example.com")` (line 23)
- **Cleanup**: **Manual** - explicit `DELETE FROM users WHERE user_id = ?` in teardown (lines 124-128)
- **Isolation**: **Partial** - manual cleanup per test, but shares `data/dev.db`

### 2.2 Database Access Patterns

**Pattern 1: Direct Database() instantiation (most common)**
- Used by: `test_ingestion_auth.py`, `test_logout_and_delete_account.py`, `test_youtube_ingest.py`, `test_drive_ingest.py`, `test_generate_blog_pipeline.py`
- Result: All share `data/dev.db`
- Problem: No isolation, UNIQUE constraint violations

**Pattern 2: Mocked ensure_db() (no actual DB)**
- Used by: `test_auth_routes.py`
- Result: No database operations
- Problem: None (tests are designed to not use DB)

**Pattern 3: Temp file per test (isolated)**
- Used by: `test_sessions.py`, `test_pipeline_events.py`
- Result: Each test gets its own SQLite file
- Problem: None (proper isolation)

---

## 3. Which Backend Do Tests Actually Hit?

### 3.1 SQLite vs D1 vs Both?

**Answer: SQLite-only**

**Evidence**:

1. **All tests use `Database()` with no arguments**:
   - `Database()` constructor (line 142-167 of `database.py`) checks for D1 binding
   - If no D1 binding (no `prepare` method), falls back to SQLite
   - Tests never pass a D1 binding, so all use SQLite

2. **No D1/Worker references in tests**:
   - No imports of `wrangler`, `env.DB`, `asgi.fetch`, or Cloudflare bindings
   - No environment variables that would enable D1
   - No mocks of Cloudflare Workers runtime

3. **Default SQLite path**:
   - `Database()` defaults to `data/dev.db` (line 158 of `database.py`)
   - All tests that don't set `LOCAL_SQLITE_PATH` use this shared file

4. **Integration tests use local server, not Workers**:
   - `tests/integration/test_server.py` tests against `http://localhost:8000`
   - This is the local FastAPI server (`run_api.py`), not `wrangler dev`
   - Server still uses SQLite (not D1)

### 3.2 Summary Table

| Backend Type | Used in Tests? | How? | Examples |
|--------------|----------------|------|----------|
| **SQLite (shared file)** | ✅ **Yes** | `Database()` → `data/dev.db` | Most tests |
| **SQLite (temp file)** | ✅ **Yes** | `Database()` with `LOCAL_SQLITE_PATH` set to temp path | `test_sessions.py`, `test_pipeline_events.py` |
| **D1 (Cloudflare Workers)** | ❌ **No** | Never used | None |
| **Live Worker (`wrangler dev`)** | ❌ **No** | Never tested | None |
| **Mocked D1 bindings** | ❌ **No** | No mocks of `env.DB` | None |

**Conclusion**: **100% SQLite-based tests, 0% D1/Worker tests**

---

## 4. UNIQUE Constraint Root Cause

### 4.1 The Problem

**Error Message**:
```
sqlite3.IntegrityError: UNIQUE constraint failed: users.google_id
```

**When It Occurs**:
- When multiple tests run in the same test session
- Tests create users with `google_id=None` or `github_id=None`
- These `None` values are converted to empty strings `""`
- Multiple tests inserting `""` for `google_id` violates UNIQUE constraint

### 4.2 Root Cause Analysis

#### Step 1: Schema Has UNIQUE Constraints

**File**: `migrations/schema.sql` (lines 6-7)
```sql
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    github_id TEXT UNIQUE,  -- ← UNIQUE constraint
    google_id TEXT UNIQUE,  -- ← UNIQUE constraint
    email TEXT NOT NULL UNIQUE,
    ...
);
```

**Impact**: Only one row can have `google_id = ""` and only one row can have `github_id = ""`.

#### Step 2: `create_user()` Converts `None` to `""`

**File**: `src/workers/api/database.py` (lines 605-609)
```python
async def create_user(db: Database, user_id: str, *, github_id: Optional[str] = None, google_id: Optional[str] = None, email: Optional[str] = None) -> Dict[str, Any]:
    """Create a new user."""
    # D1 doesn't accept Python None - convert to empty string for optional fields
    github_id_val = github_id if github_id is not None else ""  # ← None becomes ""
    google_id_val = google_id if google_id is not None else ""  # ← None becomes ""
    email_val = email if email is not None else ""
```

**Rationale**: "D1 doesn't accept Python None" - but this affects SQLite too.

**Impact**: When tests pass `github_id=None` or `google_id=None`, they become `""` in the database.

#### Step 3: Multiple Tests Create Users with `None`

**Tests that create users with `github_id=None` or `google_id=None`**:

1. `test_ingestion_auth.py` (line 24):
   ```python
   await create_user(db, user_id=user_id, github_id=None, email=email)
   ```

2. `test_youtube_ingest.py` (lines 76, 148, 254, 354, 467, 555):
   ```python
   await create_user(db, user_id=user_id, github_id=None, email=f"{user_id}@example.com")
   ```

3. `test_drive_ingest.py` (lines 87, 105, 148, 195):
   ```python
   await create_user(db, user_id=user_id, github_id=None, email=f"{user_id}@example.com")
   ```

4. `test_logout_and_delete_account.py` (line 36) - **FIXED**:
   ```python
   github_id = f"github_{uuid.uuid4()}"  # ← Now uses unique value
   await create_user(db, user_id=user_id, github_id=github_id, google_id=None, email=email)
   ```

**Impact**: Multiple tests create users with `google_id=""` and `github_id=""`, violating UNIQUE constraints.

#### Step 4: No Test Isolation

**Problem**: All tests share the same `data/dev.db` file.

**Evidence**:
- Most tests use `Database()` with no arguments → defaults to `data/dev.db`
- No fixtures clean up the database between tests
- No transactions that roll back after each test
- Only 2 test files use temp files for isolation (`test_sessions.py`, `test_pipeline_events.py`)

**Impact**: When test A creates a user with `google_id=""`, and test B tries to create another user with `google_id=""`, it fails with UNIQUE constraint violation.

### 4.3 Specific Failure Scenario

**Example**: Running `test_logout_and_delete_account.py` after `test_ingestion_auth.py`:

1. **Test 1** (`test_ingestion_auth.py::authed_client` fixture):
   - Creates user with `github_id=None, google_id=None`
   - `create_user()` converts to `github_id="", google_id=""`
   - Inserts into `data/dev.db`
   - **No cleanup** - user remains in database

2. **Test 2** (`test_logout_and_delete_account.py::authed_client` fixture):
   - Tries to create user with `github_id=github_{uuid}, google_id=None`
   - `create_user()` converts `google_id=None` to `google_id=""`
   - Tries to insert into `data/dev.db`
   - **FAILS**: UNIQUE constraint violation on `google_id` (already exists from Test 1)

### 4.4 Why Some Tests Don't Fail

**Tests that use temp files** (`test_sessions.py`, `test_pipeline_events.py`):
- Each test gets its own SQLite file
- No shared state between tests
- **No UNIQUE constraint violations**

**Tests that manually clean up** (`test_youtube_ingest.py`, `test_generate_blog_pipeline.py`):
- Explicit `DELETE FROM users WHERE user_id = ?` in teardown
- **May still fail** if cleanup doesn't run (test crashes, fixture doesn't yield, etc.)

**Tests that mock `ensure_db()`** (`test_auth_routes.py`):
- Don't actually create users
- **No UNIQUE constraint violations**

---

## 5. Isolation Strategy Options (Analysis Only)

### 5.1 Option 1: In-Memory SQLite Per Test

**Approach**: Use `:memory:` SQLite database for each test

**Implementation**:
```python
@pytest.fixture
def db():
    os.environ["LOCAL_SQLITE_PATH"] = ":memory:"
    db = Database()
    yield db
    # No cleanup needed - in-memory DB is destroyed when connection closes
```

**Pros**:
- ✅ Fastest (no disk I/O)
- ✅ Perfect isolation (each test gets fresh DB)
- ✅ No cleanup needed
- ✅ Keeps tests in SQLite-land (no D1 required)

**Cons**:
- ⚠️ Requires modifying all test fixtures
- ⚠️ May need to apply migrations per test (or use schema setup fixture)

**Invasiveness**: **Medium** - requires updating all test fixtures, but no app code changes

**Recommendation**: **Good option** if we can standardize fixture pattern

---

### 5.2 Option 2: Temp File Per Test (Like `test_sessions.py`)

**Approach**: Use `tmp_path` fixture to create unique SQLite file per test

**Implementation**:
```python
@pytest.fixture
def isolated_db(tmp_path):
    db_path = tmp_path / "test.db"
    os.environ["LOCAL_SQLITE_PATH"] = str(db_path)
    db = Database()
    yield db
    # Cleanup handled by pytest (tmp_path is cleaned up)
```

**Pros**:
- ✅ Perfect isolation (each test gets fresh DB file)
- ✅ Already proven to work (`test_sessions.py`, `test_pipeline_events.py`)
- ✅ Automatic cleanup (pytest handles `tmp_path`)
- ✅ Keeps tests in SQLite-land (no D1 required)

**Cons**:
- ⚠️ Slightly slower than in-memory (disk I/O)
- ⚠️ Requires modifying all test fixtures
- ⚠️ May need to apply migrations per test

**Invasiveness**: **Medium** - requires updating all test fixtures, but no app code changes

**Recommendation**: **Best option** - already used successfully in 2 test files

---

### 5.3 Option 3: Truncate Tables in Fixture

**Approach**: Add a fixture that truncates `users` and `user_sessions` tables before each test

**Implementation**:
```python
@pytest.fixture(autouse=True)
def clean_db():
    """Clean up users and sessions before each test."""
    db = Database()
    asyncio.run(db.execute("DELETE FROM user_sessions"))
    asyncio.run(db.execute("DELETE FROM users"))
    yield
    # Optional: cleanup after test too
```

**Pros**:
- ✅ Minimal changes (one fixture, autouse=True)
- ✅ Works with existing `data/dev.db` approach
- ✅ Keeps tests in SQLite-land (no D1 required)

**Cons**:
- ⚠️ Doesn't solve UNIQUE constraint for `google_id=""` / `github_id=""` (still only one `""` allowed)
- ⚠️ May have FK constraint issues if cleanup order is wrong
- ⚠️ Slower (DELETE operations)
- ⚠️ Doesn't help if tests run in parallel

**Invasiveness**: **Low** - only requires one fixture, but doesn't fully solve the problem

**Recommendation**: **Not sufficient** - doesn't address root cause (UNIQUE constraint on empty strings)

---

### 5.4 Option 4: Always Generate Unique Provider IDs in Tests

**Approach**: Modify test helpers to always generate unique `github_id` / `google_id` values

**Implementation**:
```python
# In test helper or fixture
def create_test_user(db, user_id, email, **kwargs):
    github_id = kwargs.get('github_id') or f"github_{uuid.uuid4()}"
    google_id = kwargs.get('google_id') or f"google_{uuid.uuid4()}"
    return create_user(db, user_id=user_id, github_id=github_id, google_id=google_id, email=email, **kwargs)
```

**Pros**:
- ✅ Solves UNIQUE constraint violations
- ✅ Minimal changes (helper function, update test calls)
- ✅ Keeps tests in SQLite-land (no D1 required)
- ✅ Works with existing `data/dev.db` approach

**Cons**:
- ⚠️ Doesn't provide true isolation (tests still share DB)
- ⚠️ May have other UNIQUE constraint issues (e.g., email collisions)
- ⚠️ Tests may interfere with each other in other ways

**Invasiveness**: **Low** - requires helper function and updating test calls

**Recommendation**: **Partial solution** - fixes UNIQUE constraint but doesn't provide full isolation

---

### 5.5 Option 5: Transaction Rollback Per Test

**Approach**: Wrap each test in a transaction that rolls back after the test

**Implementation**:
```python
@pytest.fixture
def db_transaction():
    db = Database()
    # Start transaction
    asyncio.run(db.execute("BEGIN TRANSACTION"))
    yield db
    # Rollback transaction
    asyncio.run(db.execute("ROLLBACK"))
```

**Pros**:
- ✅ Perfect isolation (each test gets fresh state)
- ✅ Fast (no disk I/O for cleanup)
- ✅ Keeps tests in SQLite-land (no D1 required)

**Cons**:
- ⚠️ SQLite transactions may not work as expected with `Database()` wrapper
- ⚠️ Requires careful handling of DDL (CREATE TABLE) vs DML (INSERT/UPDATE)
- ⚠️ May need to refactor `Database()` to support transaction management

**Invasiveness**: **High** - may require changes to `Database` class

**Recommendation**: **Complex** - SQLite transaction behavior with the current `Database()` wrapper is unclear

---

### 5.6 Recommended Approach: Hybrid (Option 2 + Option 4)

**Best Solution**: Combine temp file per test (Option 2) with unique provider IDs (Option 4)

**Why**:
1. **Temp file per test** provides true isolation (no shared state)
2. **Unique provider IDs** prevents UNIQUE constraint violations even if tests share DB (defense in depth)
3. **Already proven** - `test_sessions.py` and `test_pipeline_events.py` use this pattern successfully

**Implementation Strategy**:
1. Create a shared `isolated_db` fixture in `conftest.py` (like `test_sessions.py`)
2. Update all test fixtures to use `isolated_db`
3. Create a helper function `create_test_user()` that always generates unique provider IDs
4. Update all test calls to use the helper

**Invasiveness**: **Medium** - requires updating fixtures and test calls, but no app code changes

---

## 6. Worker/D1 Integration Tests (If Any)

### 6.1 Search Results

**Searched for**: `wrangler`, `asgi.fetch`, `cloudflare`, `D1`, `env.DB`, `localhost:8787`

**Results**: **No matches found in test files**

### 6.2 Integration Test Analysis

**File**: `tests/integration/test_server.py`

**What it tests**:
- Tests against a running server at `http://localhost:8000`
- This is the **local FastAPI server** (`run_api.py`), not `wrangler dev`
- Server still uses SQLite (not D1)

**Evidence**:
```python
BASE_URL = "http://localhost:8000"  # Local server, not Workers
```

**File**: `tests/integration/test_local.py`

**What it tests**:
- Basic app import and TestClient creation
- No actual HTTP requests or Workers runtime

### 6.3 Conclusion

**Answer**: **No Worker/D1 integration tests exist**

**What exists**:
- ✅ Integration tests against local FastAPI server (SQLite-backed)
- ❌ No tests against `wrangler dev` (Cloudflare Workers)
- ❌ No tests using D1 bindings
- ❌ No tests using `asgi.fetch` entrypoint

**Implication**: All tests are SQLite-based, even "integration" tests. There is no test coverage for Cloudflare Workers / D1 runtime behavior.

---

## 7. Summary & Recommendation

### 7.1 Current State

**Are tests currently only exercising SQLite?**
- ✅ **Yes** - 100% of tests use SQLite
- ❌ **No** - 0% of tests use D1 or Cloudflare Workers

**Evidence**:
- All tests use `Database()` with no D1 binding → falls back to SQLite
- Default SQLite path: `data/dev.db` (shared across most tests)
- No references to `wrangler`, `env.DB`, or Cloudflare bindings in tests
- Integration tests hit local FastAPI server (still SQLite), not Workers

### 7.2 Why Are We Seeing UNIQUE Constraint Failures?

**Root Cause**: **Three factors combine to cause failures**:

1. **Schema has UNIQUE constraints** on `github_id` and `google_id` (only one `""` allowed)
2. **`create_user()` converts `None` to `""`** (D1 compatibility, but affects SQLite too)
3. **No test isolation** - most tests share `data/dev.db`, so multiple tests inserting `""` violates UNIQUE constraint

**Specific Failure**:
- Test A creates user with `google_id=None` → becomes `google_id=""` in DB
- Test B tries to create user with `google_id=None` → tries to insert `google_id=""` → **FAILS** (UNIQUE constraint)

**Why Some Tests Don't Fail**:
- Tests using temp files (`test_sessions.py`, `test_pipeline_events.py`) have isolation
- Tests that manually clean up (`test_youtube_ingest.py`) may avoid collisions if cleanup runs
- Tests that mock `ensure_db()` don't create users

### 7.3 Most Practical Solution

**Recommended Approach**: **Temp file per test + unique provider IDs (Hybrid)**

**Why This Is Best**:
1. **Proven to work** - `test_sessions.py` and `test_pipeline_events.py` already use this pattern successfully
2. **True isolation** - each test gets fresh database, no shared state
3. **No app code changes** - only test fixtures and helpers need updates
4. **Keeps tests in SQLite-land** - no D1/Workers required for unit tests
5. **Defense in depth** - unique provider IDs prevent collisions even if tests accidentally share DB

**Implementation Steps**:
1. Create shared `isolated_db` fixture in `tests/conftest.py`:
   ```python
   @pytest.fixture
   def isolated_db(tmp_path):
       db_path = tmp_path / "test.db"
       os.environ["LOCAL_SQLITE_PATH"] = str(db_path)
       db = Database()
       yield db
       # Cleanup handled by pytest
   ```

2. Create helper function for test user creation:
   ```python
   def create_test_user(db, user_id, email, **kwargs):
       """Create a test user with unique provider IDs."""
       github_id = kwargs.get('github_id') or f"github_{uuid.uuid4()}"
       google_id = kwargs.get('google_id') or f"google_{uuid.uuid4()}"
       return create_user(db, user_id=user_id, github_id=github_id, google_id=google_id, email=email, **kwargs)
   ```

3. Update all test fixtures to use `isolated_db`:
   ```python
   @pytest.fixture
   def authed_client(isolated_db):  # ← Use isolated_db
       # ... setup code using isolated_db ...
   ```

4. Update all `create_user()` calls to use `create_test_user()` helper

**Estimated Impact**:
- **Files to modify**: ~10 test files
- **Lines of code**: ~50-100 lines (mostly fixture updates)
- **Risk**: Low (pattern already proven in 2 test files)
- **Time**: 1-2 hours

### 7.4 What NOT to Do

**Don't pull Cloudflare/D1 into unit tests**:
- Unit tests should remain SQLite-based for speed and simplicity
- D1/Workers testing should be separate integration tests (if needed)
- Mixing D1 into unit tests would slow them down and add complexity

**Don't rely on manual cleanup**:
- Manual `DELETE` statements are error-prone and don't prevent collisions
- Tests may crash before cleanup runs
- Doesn't solve UNIQUE constraint on empty strings

**Don't use shared `data/dev.db` without isolation**:
- Current approach causes flaky tests
- Tests interfere with each other
- Not suitable for parallel test execution

---

**Document Version**: 1.0  
**Last Updated**: 2025-01-20  
**Status**: Analysis Complete - Ready for Implementation

