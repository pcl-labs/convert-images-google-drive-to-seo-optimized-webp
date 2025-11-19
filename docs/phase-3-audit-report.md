# Phase 3 Audit Report: Auth, JWT, Sessions & Dependencies

## JWT Implementation

### Location
`src/workers/api/jwt.py`

### Public API
- `encode(payload: Dict[str, Any], key: str, algorithm: str = "HS256") -> str`
- `decode(token: str, key: str, algorithms: Optional[List[str]] = None) -> Dict[str, Any]`
- `ExpiredSignatureError` (exception)
- `InvalidTokenError` (exception)

### Algorithm Support
- **HS256 only**: Pure Python implementation using standard library modules
- Uses: `hmac`, `hashlib`, `base64`, `json`, `time`, `datetime`
- No C-extensions or external crypto libraries

### Security Features
- ✅ Uses `hmac.compare_digest()` for signature verification (timing-attack resistant)
- ✅ Validates `alg` header against allowed `algorithms` parameter
- ✅ Enforces expiration (`exp` claim) when present
- ✅ Supports `iat` (issued-at) claim
- ⚠️ `algorithms` parameter can be `None` (allows any algorithm) - should default to `["HS256"]` for safety

### Claims Handling
- `exp`: Enforced - raises `ExpiredSignatureError` if expired
- `iat`: Accepted and stored, but not validated
- `nbf`: Not currently handled (would need to be added if required)
- Datetime objects: Automatically converted to Unix timestamps during encoding

### Usage Locations
- **Token Creation**: `src/workers/api/auth.py::generate_jwt_token()` (line 118)
- **Token Verification**: `src/workers/api/auth.py::verify_jwt_token()` (line 137)
- **Middleware**: `src/workers/api/middleware.py::AuthCookieMiddleware` (line 132)

### Tests
- Location: `tests/test_jwt.py`
- Coverage: Basic encode/decode, expiration, signature validation, algorithm validation, case insensitivity
- Missing: `iat`/`nbf` validation tests, edge cases for malformed tokens

---

## Auth & OAuth Flows

### GitHub OAuth Flow
**Location**: `src/workers/api/auth.py`

1. **Authorization URL Generation**: `get_github_oauth_url()` (line 577)
   - Builds OAuth URL with state parameter
   - Uses `secrets.token_urlsafe()` for state generation

2. **Code Exchange**: `exchange_github_code()` (line 201)
   - **Method**: HTTP POST to `https://github.com/login/oauth/access_token`
   - **Client**: `AsyncSimpleClient` (pure HTTP, no auth libraries)
   - Returns access token

3. **User Info Fetch**: `get_github_user_info()` (line 152)
   - **Method**: HTTP GET to `https://api.github.com/user`
   - **Client**: `AsyncSimpleClient`
   - Returns user profile data

4. **Email Fetch**: `get_github_primary_email()` (line 171)
   - **Method**: HTTP GET to `https://api.github.com/user/emails`
   - Falls back to synthesized email if unavailable

5. **Authentication**: `authenticate_github()` (line 240)
   - Orchestrates the flow
   - Creates/updates user in D1
   - Generates JWT token

**Status**: ✅ Pure HTTP-based, no third-party auth libraries

### Google OAuth Flow
**Location**: `src/workers/api/auth.py`

1. **Authorization URL Generation**: `get_google_login_oauth_url()` (line 303)
   - Builds OAuth URL with state parameter
   - Uses `secrets.token_urlsafe()` for state generation

2. **Code Exchange**: `exchange_google_login_code()` (line 323)
   - **Method**: HTTP POST to `https://oauth2.googleapis.com/token`
   - **Client**: `AsyncSimpleClient` (pure HTTP, no auth libraries)
   - Returns ID token and access token

3. **ID Token Verification**: `_verify_google_id_token()` (line 378)
   - **Method**: HTTP GET to `https://oauth2.googleapis.com/tokeninfo?id_token=...`
   - **Client**: `AsyncSimpleClient`
   - Validates audience matches `google_client_id`
   - Returns verified token payload

4. **User Info Fetch**: `get_google_user_info()` (line 357)
   - **Method**: HTTP GET to `https://openidconnect.googleapis.com/v1/userinfo`
   - **Client**: `AsyncSimpleClient`
   - Fallback if email not in ID token

5. **Authentication**: `authenticate_google()` (line 400)
   - Orchestrates the flow
   - Creates/updates user in D1
   - Generates JWT token

**Status**: ✅ Pure HTTP-based, no `google-auth` or `google-auth-oauthlib` libraries used

### Stub Package
- **Location**: `google_auth_oauthlib/flow.py`
- **Status**: Stub implementation exists but is **not imported or used** anywhere in the codebase
- **Action**: Can be removed if not needed for compatibility

---

## Sessions

### Storage
- **Table**: `user_sessions` in D1 database
- **Schema Location**: `migrations/schema.sql` (lines 45-59)
- **Schema Functions**: `src/workers/api/database.py`
  - `ensure_sessions_schema()` (line 1691)
  - `create_user_session()` (line 1873)
  - `get_user_session()` (line 1909)
  - `touch_user_session()` (line 1919)
  - `delete_user_session()` (line 1953)

### Table Structure
```sql
CREATE TABLE user_sessions (
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
```

### Cookie Management
- **Session Cookie**: `session_id` (configurable via `settings.session_cookie_name`)
- **JWT Cookie**: `access_token`
- **Security Flags**:
  - `httponly=True` ✅
  - `secure=is_secure` (based on request scheme) ✅
  - `samesite="lax"` ✅
  - `max_age` set based on TTL

### Cookie Setting Locations
1. **Session Cookie**: `src/workers/api/public.py::_issue_session_cookie()` (line 91)
2. **JWT Cookie**: `src/workers/api/public.py` callback handlers (lines 416, 467)
3. **Cookie Deletion**: `src/workers/api/middleware.py::SessionMiddleware` (line 115)

### Session vs JWT Relationship
- **JWT (`access_token`)**: Primary authentication token, contains user claims
- **Session (`session_id`)**: Browser session tracking, stored in D1, used for:
  - Activity tracking (`last_seen_at`)
  - Notification cursor (`last_notification_id`)
  - Session revocation (`revoked_at`)
  - Metadata storage (`extra` field)

**Architecture**: Sessions and JWTs coexist - JWT provides stateless auth, session provides stateful tracking

### Middleware
- **SessionMiddleware**: `src/workers/api/middleware.py` (line 35)
  - Loads session from cookie
  - Validates expiration
  - Touches session on activity
  - Clears invalid/expired sessions
- **AuthCookieMiddleware**: `src/workers/api/middleware.py` (line 125)
  - Loads JWT from `access_token` cookie
  - Verifies token and populates `request.state.user`
  - Validates session user matches JWT user

### Encryption
- **Status**: ✅ No `encryption_key` references found in codebase
- **Storage**: D1 provides encryption at rest (Cloudflare-managed)
- **Field-level encryption**: Not used (removed in previous phases)

---

## Dependencies

### Auth/Crypto Dependencies in pyproject.toml
```toml
"google-auth>=2.43.0,<2.44.0",
"google-auth-oauthlib>=1.2.2,<1.3.0",
```

### Auth/Crypto Dependencies in requirements.txt
```
google-auth-oauthlib
```

### Usage Analysis
- **google-auth**: ❌ Not imported anywhere
- **google-auth-oauthlib**: ❌ Not imported anywhere (stub exists but unused)
- **cryptography**: ❌ Not in dependencies (removed in previous phases)
- **pyjwt**: ❌ Not in dependencies (replaced with pure-Python implementation)

### Status
✅ **Safe to remove**: `google-auth` and `google-auth-oauthlib` are not used in the codebase

### Remaining Dependencies (Still Needed)
- `fastapi`, `starlette`, `pydantic` - Core framework
- `httpx` - HTTP client (used by `AsyncSimpleClient`)
- `tqdm` - Progress bars
- `openai` - OpenAI API client
- Standard library only for JWT (no external crypto libs)

---

## Summary

### Strengths
1. ✅ Pure Python JWT implementation (Cloudflare Workers compatible)
2. ✅ HTTP-based OAuth flows (no heavyweight auth libraries)
3. ✅ Secure cookie flags consistently applied
4. ✅ No field-level encryption dependencies
5. ✅ Comprehensive JWT tests

### Areas for Improvement
1. ⚠️ JWT `decode()` should default `algorithms` to `["HS256"]` for safety
2. ⚠️ Add tests for `iat`/`nbf` claims if needed
3. ⚠️ Remove unused `google-auth` dependencies
4. ⚠️ Document session architecture more clearly
5. ⚠️ Consider removing `google_auth_oauthlib/` stub if truly unused

### Next Steps
1. Harden JWT implementation (default algorithms)
2. Remove unused dependencies
3. Add documentation for session model
4. Update project documentation

