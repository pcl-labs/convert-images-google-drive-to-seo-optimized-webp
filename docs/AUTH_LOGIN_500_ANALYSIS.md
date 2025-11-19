# Login/Auth Flow 500 Error Analysis Report

**Date:** 2025-11-19  
**Context:** Cloudflare Python Worker environment (`wrangler dev`)  
**Goal:** Identify likely sources of `Internal Server Error` (500) in login/auth flows and protected routes

---

## Executive Summary

This analysis maps all authentication and login-related routes, templates, middleware, and dependencies to identify potential 500 error sources similar to those fixed for the `/` route. Key findings:

1. **DB initialization failures** in OAuth callback routes (not protected by public-route handling)
2. **`request.client.host` access** in multiple places without proper null checks
3. **Template context issues** - all templates use `url_for` which should work, but need verification
4. **AuthCookieMiddleware DB access** - may fail for anonymous users during token validation
5. **Missing route names** - some redirects use hardcoded URLs instead of `url_for`

---

## 1. Auth & Login Route Map

### 1.1 Public Auth Routes (No Authentication Required)

| Route | Method | Handler | File | Auth Level | Notes |
|-------|--------|---------|------|------------|-------|
| `/auth/github/start` | GET | `github_auth_start` | `public.py:337` | Public | Redirects to GitHub OAuth |
| `/auth/github/start` | POST | `github_auth_start_post` | `public.py:347` | Public | CSRF-protected form submission |
| `/auth/google/login/start` | GET | `google_login_start` | `public.py:368` | Public | Redirects to Google OAuth |
| `/auth/google/login/start` | POST | `google_login_start_post` | `public.py:378` | Public | CSRF-protected form submission |
| `/auth/github/callback` | GET | `github_callback` | `public.py:399` | Public | **⚠️ Calls `ensure_db()`** |
| `/auth/google/login/callback` | GET | `google_login_callback` | `public.py:444` | Public | **⚠️ Calls `ensure_db()`** |
| `/auth/logout` | GET | `logout_get` | `public.py:513` | Public | **⚠️ Calls `ensure_db()` if session exists** |
| `/login` | GET | `login_page` | `web.py:1041` | Public | Renders `auth/login.html` |
| `/signup` | GET | `signup_page` | `web.py:1046` | Public | Renders `auth/login.html` |

### 1.2 Protected Auth Routes (Requires Authentication)

| Route | Method | Handler | File | Auth Level | Notes |
|-------|--------|---------|------|------------|-------|
| `/auth/google/start` | GET | `google_auth_start` | `protected.py:774` | Authenticated | For linking Google integrations |
| `/auth/google/callback` | GET | `google_auth_callback` | `protected.py:835` | Authenticated | **⚠️ Calls `ensure_db()`** |
| `/auth/github/status` | GET | `github_link_status` | `protected.py:755` | Authenticated | **⚠️ Calls `ensure_db()`** |
| `/auth/google/status` | GET | `google_link_status` | `protected.py:868` | Authenticated | **⚠️ Calls `ensure_db()`** |
| `/auth/providers/status` | GET | `providers_status` | `protected.py:879` | Authenticated | **⚠️ Calls `ensure_db()`** |
| `/auth/me` | GET | `get_current_user_info` | `protected.py:898` | Authenticated | **⚠️ Calls `ensure_db()` if needed** |
| `/auth/keys` | POST | `create_api_key_endpoint` | `protected.py:936` | Authenticated | **⚠️ Calls `ensure_db()`** |

### 1.3 Protected Application Routes (Requires Authentication)

All routes under `/dashboard/*` require `Depends(get_current_user)`:
- `/dashboard` - Main dashboard page
- `/dashboard/documents` - Document list
- `/dashboard/documents/{document_id}` - Document detail
- `/dashboard/jobs` - Job list
- `/dashboard/jobs/{job_id}` - Job detail
- `/dashboard/integrations` - Integrations page
- `/dashboard/settings` - Settings page
- `/dashboard/account` - Account page
- `/dashboard/activity` - Activity feed

**All of these call `ensure_db()` at the start of the handler.**

---

## 2. Auth Flow Traces

### 2.1 Anonymous User → Login Start → OAuth Provider

**Flow:**
1. User visits `/login` → `login_page()` → Renders `auth/login.html`
2. User clicks "Continue with GitHub" → POST `/auth/github/start` (with CSRF token)
3. `github_auth_start_post()` validates CSRF, calls `_get_github_oauth_redirect()`
4. Returns `RedirectResponse` to GitHub OAuth URL with state cookie

**Potential Issues:**
- ✅ CSRF validation uses `request.client.host` with null check (`public.py:352`)
- ✅ No DB access in this flow

### 2.2 OAuth Callback → Session Creation → Dashboard Redirect

**Flow (GitHub):**
1. GitHub redirects to `/auth/github/callback?code=...&state=...`
2. `github_callback()` validates state cookie
3. **⚠️ `db = ensure_db()` - NO ERROR HANDLING if DB fails**
4. Calls `authenticate_github(db, code)` which:
   - Exchanges code for access token (HTTP request)
   - Gets user info from GitHub API (HTTP request)
   - **Calls multiple DB operations:**
     - `get_user_by_github_id(db, github_id)`
     - `get_user_by_email(db, email)` (if needed)
     - `create_user(db, ...)` or `update_user_identity(db, ...)`
5. Generates JWT token
6. **⚠️ `await _issue_session_cookie(...)` calls `ensure_db()` again and `create_user_session()`**
7. Sets cookies and redirects to `/dashboard`

**Potential Issues:**
- ❌ **`ensure_db()` at line 406 has no try/except - will 500 if DB unavailable**
- ❌ **`_issue_session_cookie()` at line 425 calls `ensure_db()` again (line 115) - no error handling**
- ❌ **`create_user_session()` may fail if DB is unavailable**
- ⚠️ **`request.client.host` access at line 120 in `_issue_session_cookie()` - has null check ✅**

**Flow (Google Login):**
- Similar structure to GitHub callback
- Same issues: `ensure_db()` at line 451, `_issue_session_cookie()` at line 476

### 2.3 Protected Route Access (e.g., `/dashboard`)

**Flow:**
1. User requests `/dashboard`
2. `AuthCookieMiddleware` runs:
   - Extracts JWT from `access_token` cookie
   - Verifies JWT token
   - **⚠️ If email missing, calls `ensure_db()` at line 258 to fetch user**
   - Sets `request.state.user`
3. `get_current_user()` dependency checks `request.state.user`
4. Handler `dashboard()` calls `ensure_db()` at line 1060
5. Fetches jobs, stats from DB
6. Renders `dashboard/index.html` template

**Potential Issues:**
- ❌ **`AuthCookieMiddleware` calls `ensure_db()` at line 258 without error handling - will 500 if DB unavailable**
- ❌ **All protected route handlers call `ensure_db()` without try/except**
- ✅ Templates use `url_for` which should work (we fixed this)

---

## 3. Template Map for Auth/Protected Pages

### 3.1 Login/Auth Templates

| Template | Base Template | Key Variables | `url_for` Usage | Notes |
|----------|---------------|---------------|-----------------|-------|
| `auth/login.html` | `base_public.html` | `request`, `csrf_token`, `view_mode`, `error` | ✅ `url_for('static', path='logos/quill-logo.png')` | Used by `/login` and `/signup` |
| `home.html` | `base_public.html` | `request` | ✅ `url_for('static', path='logos/quill-logo.png')` | Used by `/` |

### 3.2 Protected Page Templates

| Template | Base Template | Key Variables | `url_for` Usage | Notes |
|----------|---------------|---------------|-----------------|-------|
| `dashboard/index.html` | `base.html` | `request`, `user`, `stats`, `csrf_token`, `drive_connected`, `content_schema_choices` | ❓ No direct `url_for` in template | Uses `base.html` which has `url_for` |
| `base.html` | N/A | `request` (implicit) | ✅ Multiple `url_for('static', ...)` calls | Base for all authenticated pages |
| `base_public.html` | N/A | `request` (implicit) | ✅ Multiple `url_for('static', ...)` calls | Base for public pages |

### 3.3 Template Context Analysis

**All templates that use `url_for`:**
- ✅ `base_public.html` - Uses `url_for` for static assets (7 instances)
- ✅ `base.html` - Uses `url_for` for static assets (7 instances)
- ✅ `auth/login.html` - Uses `url_for` for logo (1 instance)
- ✅ `home.html` - Uses `url_for` for logo (1 instance)
- ✅ `components/layout/sidebar.html` - Uses `url_for` for logo (1 instance)

**Template Context Requirements:**
- All `TemplateResponse()` calls must include `{"request": request}` in context
- `url_for` function extracts `request` from Jinja context via `@pass_context`
- ✅ All template rendering calls include `request` in context

**Potential Issues:**
- ✅ `url_for` registration is correct (we fixed this)
- ⚠️ Need to verify all template rendering includes `request` in context

---

## 4. Middleware & Dependencies Analysis

### 4.1 SessionMiddleware

**Location:** `middleware.py:67-228`

**Behavior:**
- Extracts `session_id` from cookie
- **✅ Fixed:** Catches `HTTPException(500)` from `ensure_db()` for public routes
- Loads session from DB if session_id exists
- Sets `request.state.session` and `request.state.session_user_id`

**Potential Issues:**
- ✅ Already fixed for public routes
- ⚠️ For protected routes, DB failure will still 500 (but protected routes require DB anyway)

### 4.2 FlashMiddleware

**Location:** `middleware.py:230-268`

**Behavior:**
- Clears flash messages from session after request
- **✅ Fixed:** Catches `HTTPException(500)` from `ensure_db()` for public routes
- Updates session `extra` field in DB

**Potential Issues:**
- ✅ Already fixed for public routes

### 4.3 AuthCookieMiddleware

**Location:** `middleware.py:230-300`

**Behavior:**
- Extracts JWT from `access_token` cookie
- Verifies JWT token
- **⚠️ If email missing from JWT, calls `ensure_db()` at line 258 to fetch user from DB**
- Sets `request.state.user`

**Potential Issues:**
- ❌ **`ensure_db()` at line 258 has NO error handling - will 500 if DB unavailable**
- ⚠️ This runs for ALL requests (including public routes) if they have an `access_token` cookie
- ⚠️ If DB fails, user won't be set, but request continues (may cause 401 later)

### 4.4 RateLimitMiddleware

**Location:** `middleware.py:306-380`

**Behavior:**
- Gets client ID for rate limiting
- **✅ Fixed:** Properly handles `request.client` being None
- Falls back to `CF-Connecting-IP` header
- Uses in-memory rate limit tracking

**Potential Issues:**
- ✅ Already handles `request.client` being None correctly

### 4.5 get_current_user Dependency

**Location:** `deps.py:69-76`

**Behavior:**
- Checks `request.state.user` (set by `AuthCookieMiddleware`)
- Raises `HTTPException(401)` if user is None

**Potential Issues:**
- ✅ No DB access - safe
- ⚠️ Depends on `AuthCookieMiddleware` setting `request.state.user` correctly

### 4.6 ensure_db Dependency

**Location:** `deps.py:32-51`

**Behavior:**
- Lazily initializes `Database()` instance
- Raises `HTTPException(500)` if initialization fails

**Potential Issues:**
- ⚠️ **Called in many places without error handling:**
  - OAuth callbacks (public routes)
  - All protected route handlers
  - `AuthCookieMiddleware` (for all requests with JWT)
  - `_issue_session_cookie()` helper

---

## 5. Likely 500 Error Sources

### 5.1 Critical Issues (High Priority)

#### Issue 1: OAuth Callback Routes Call `ensure_db()` Without Error Handling

**Location:** `public.py:406`, `public.py:451`

**Problem:**
```python
@router.get("/auth/github/callback", tags=["Authentication"])
async def github_callback(code: str, state: str, request: Request):
    # ... state validation ...
    db = ensure_db()  # ❌ NO ERROR HANDLING - will 500 if DB unavailable
    try:
        jwt_token, user = await authenticate_github(db, code)
        # ...
```

**Impact:**
- If D1 database is unavailable during OAuth callback, user gets 500 error
- User cannot complete login flow
- Similar issue in `google_login_callback()`

**Fix Required:**
- These are public routes that require DB for authentication
- Should catch `HTTPException(500)` from `ensure_db()` and return user-friendly error
- Or redirect to login page with error message

#### Issue 2: AuthCookieMiddleware Calls `ensure_db()` Without Error Handling

**Location:** `middleware.py:258`

**Problem:**
```python
if user_id and not email:
    try:
        db = ensure_db()  # ❌ NO ERROR HANDLING - will 500 if DB unavailable
        stored = await get_user_by_id(db, user_id)
        # ...
```

**Impact:**
- Runs for ALL requests that have an `access_token` cookie
- If DB is unavailable, entire request fails with 500
- Affects both public and protected routes

**Fix Required:**
- Catch `HTTPException(500)` and log warning
- Continue without email (JWT may have email anyway)
- Don't fail the entire request

#### Issue 3: `_issue_session_cookie()` Calls `ensure_db()` Without Error Handling

**Location:** `public.py:115` (called from `_issue_session_cookie()`)

**Problem:**
```python
async def _issue_session_cookie(...):
    # ...
    try:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
        await create_user_session(
            db,  # db comes from ensure_db() call in caller
            # ...
```

**Impact:**
- Called from OAuth callbacks after `ensure_db()` already succeeded
- But if DB becomes unavailable between calls, will fail
- Session cookie won't be set, but JWT cookie will be set

**Fix Required:**
- Already wrapped in try/except at line 124, but `ensure_db()` is called in caller
- Should catch DB failures gracefully and log warning
- Continue without session cookie (JWT is sufficient)

### 5.2 Medium Priority Issues

#### Issue 4: Protected Route Handlers Call `ensure_db()` Without Error Handling

**Location:** All protected route handlers (e.g., `web.py:1060`, `web.py:1124`, etc.)

**Problem:**
```python
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, page: int = 1, user: dict = Depends(get_current_user)):
    db = ensure_db()  # ❌ NO ERROR HANDLING
    # ...
```

**Impact:**
- Protected routes require DB access by design
- If DB is unavailable, should return 503 Service Unavailable, not 500
- Currently returns 500 from `ensure_db()`

**Fix Required:**
- Catch `HTTPException(500)` and re-raise as 503
- Or add retry logic for transient DB failures

#### Issue 5: `request.client.host` Access in Multiple Places

**Location:** `public.py:120`, `public.py:352`, `public.py:383`

**Problem:**
```python
ip_address=(request.client.host if request.client else None),  # ✅ Has null check
client_host = request.client.host if request.client else "-"  # ✅ Has null check
```

**Impact:**
- ✅ Most places already have null checks
- ⚠️ Need to verify all usages are safe

**Fix Required:**
- Audit all `request.client.host` usages
- Ensure all have null checks or use helper function

### 5.3 Low Priority Issues

#### Issue 6: Hardcoded Redirect URLs Instead of `url_for`

**Location:** Multiple places use `RedirectResponse(url="/dashboard")` instead of `url_for("dashboard")`

**Problem:**
- Hardcoded URLs work but don't benefit from route name validation
- If route path changes, redirects break

**Fix Required:**
- Add route names to all routes
- Use `request.url_for("route_name")` for redirects

#### Issue 7: Template Context Verification

**Location:** All `TemplateResponse()` calls

**Problem:**
- Need to verify all template rendering includes `request` in context
- Missing `request` will cause `url_for` to fail

**Fix Required:**
- Audit all `TemplateResponse()` calls
- Ensure `{"request": request}` is always included

---

## 6. Worker-Specific Assumptions

### 6.1 Filesystem Access

**Status:** ✅ Safe
- Templates are loaded from filesystem at startup
- Static files use Cloudflare Assets binding
- No runtime filesystem access

### 6.2 Blocking I/O

**Status:** ✅ Safe
- All DB operations are async
- HTTP requests use async clients
- No blocking I/O detected

### 6.3 Environment Variables

**Status:** ✅ Safe
- `apply_worker_env()` called before app creation
- Settings loaded from `os.environ`
- Error handling added in `runtime.py`

### 6.4 `request.client` Availability

**Status:** ⚠️ Partially Safe
- Most places have null checks
- `RateLimitMiddleware` has proper fallback
- Some logging code may fail if `request.client` is None

---

## 7. Recommended Fix Priority

### Phase 1: Critical (Fix First)
1. **OAuth callback routes** - Add error handling for `ensure_db()` failures
2. **AuthCookieMiddleware** - Add error handling for `ensure_db()` failures
3. **`_issue_session_cookie()`** - Ensure DB failures are handled gracefully

### Phase 2: High Priority
4. **Protected route handlers** - Catch `ensure_db()` and return 503 instead of 500
5. **Audit `request.client.host` usages** - Ensure all are safe

### Phase 3: Medium Priority
6. **Add route names** - Use `url_for` for redirects
7. **Template context verification** - Ensure all templates have `request` in context

---

## 8. Testing Recommendations

### 8.1 Test Cases to Add

1. **OAuth callback with DB unavailable:**
   - Mock `ensure_db()` to raise `HTTPException(500)`
   - Verify user-friendly error or redirect to login

2. **AuthCookieMiddleware with DB unavailable:**
   - Set `access_token` cookie with valid JWT
   - Mock `ensure_db()` to raise `HTTPException(500)`
   - Verify request continues (may get 401 from `get_current_user`)

3. **Protected route with DB unavailable:**
   - Authenticate user
   - Mock `ensure_db()` to raise `HTTPException(500)`
   - Verify 503 response instead of 500

4. **Template rendering without `request` in context:**
   - Verify `url_for` fails with clear error message

---

## 9. Summary

**Total Issues Identified:** 7
- **Critical:** 3 (OAuth callbacks, AuthCookieMiddleware, `_issue_session_cookie`)
- **Medium:** 2 (Protected routes, `request.client.host`)
- **Low:** 2 (Route names, template context)

**Key Patterns:**
1. **DB initialization failures** - Most common issue, affects OAuth callbacks and middleware
2. **Missing error handling** - Many `ensure_db()` calls lack try/except
3. **Worker-specific assumptions** - `request.client` mostly handled, but needs audit

**Next Steps:**
1. Implement fixes for critical issues (Phase 1)
2. Add tests to lock in behavior
3. Verify fixes work in `wrangler dev` environment

