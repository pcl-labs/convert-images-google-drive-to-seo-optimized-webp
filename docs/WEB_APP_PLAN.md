# Web App Plan: FastAPI + Jinja2 + HTMX (Keep Current Auth)

This plan adds a simple, scalable web frontend on top of your existing API and auth. It preserves your current GitHub OAuth → JWT cookie flow and AuthenticationMiddleware, and optionally allows Google account linking for Drive operations.

## Goals

- **Keep** existing auth: GitHub OAuth (`/auth/github/*`), JWT cookie, API keys, middleware enforcement.
- **Add** a server-rendered UI with Jinja2 and HTMX for incremental interactivity.
- **Expose** job flows (submit Drive URL(s), view status) via pages and HTMX partials.
- **Prepare** for future CMS features (RBAC, settings, audit, richer content mgmt) without changing core auth.

## Architecture Overview

- **FastAPI (existing app)**
  - HTML routes (Jinja2 templates)
  - JSON API routes (existing + new)
  - Authentication via current middleware (JWT cookie or API Key)
- **Templates / Static Assets**
  - Jinja2 templates, macros, HTMX for partial updates
  - Tailwind (CLI) for quick styling; swap later if needed
- **Async pipeline**
  - Keep Cloudflare Queues/Workers for background processing
  - Jobs/status persisted in DB; HTMX polls/updates partials

## Project Structure Additions

- app/
  - main.py (or reuse api/main.py if preferred; mount routers)
  - settings.py (reuse api/config.py if centralizing)
  - deps.py (DB, auth deps)
  - middleware.py (reuse existing in api/middleware.py)
  - routers/
    - web.py (HTML routes: login page, dashboard, job list/detail)
    - jobs.py (JSON + HTMX partial responses)
    - auth.py (optional convenience routes like `/login` redirecting to GitHub start)
  - templates/
    - base.html
    - components/
      - form.html (CSRF helper, error rendering)
      - table.html (list/pagination)
      - alert.html (flash messages)
      - modal.html (optional)
    - auth/login.html
    - jobs/dashboard.html
    - jobs/list.html
    - jobs/detail.html
    - jobs/partials/
      - row.html
      - status_badge.html
    - errors/404.html, 500.html
  - static/
    - css/ (compiled Tailwind)
    - js/ (htmx.min.js)

Note: If you prefer, keep everything in `api/` to avoid a new top-level module. The important piece is routers/templates/static separation.

## Routing and UX

- GET `/login` → Simple page with “Sign in with GitHub” → links to `/auth/github/start` (already implemented)
- GET `/` (dashboard, auth-gated) →
  - Paste one or multiple Google Drive URLs
  - Options: overwrite, skip_existing, seo_prefix, etc.
  - Recent jobs table (HTMX-enabled; progressive enhancement)
- POST `/jobs` → Enqueue job(s), returns HTMX snippet to update list/table
- GET `/jobs` → Paginated list (HTML page)
- GET `/jobs/{id}` → Detail page; HTMX fragment updates for live status
- GET `/settings` → Settings home with Linked Accounts section
- GET `/settings/accounts` → Linked Accounts management UI
- Provider linking (Google now, YouTube later):
  - GET `/auth/google/start` → Begin Google OAuth (link account)
  - GET `/auth/google/callback` → Store tokens, upsert linked account
  - GET `/auth/youtube/start` → Placeholder (future)
  - GET `/auth/youtube/callback` → Placeholder (future)
- Optional: POST `/auth/logout` (mainly for dev; production relies on JWT expiry)

HTMX patterns:
- `hx-post` for form submission, `hx-target` pointing to the list container
- `hx-swap="outerHTML"` to replace updated rows/sections
- `hx-trigger="every 2s"` on a status partial for polling

## Data Model (extensible)

Existing tables for users, api_keys, jobs, google_tokens appear to be in place. Proposed additions or confirmations:

- users (existing via GitHub OAuth)
  - id (user_id), github_id, email, role (optional)
- sessions (optional server-side sessions; current design uses JWT cookie)
- jobs (existing; extend for multiple job types)
  - id, user_id, job_type (`optimize_images`, future: `transcribe_video`), source_url, status, output_refs (json), idempotency_key, options (json), timestamps
- job_events (recommended)
  - id, job_id, type, message, data (json), created_at
- api_tokens (existing)
  - rotation and scopes later
- google_tokens (existing)
  - used if/when enabling Google account linking
- linked_accounts (new, generic provider linking)
  - id (uuid), user_id (fk → users.user_id)
  - provider (`google_drive`, later `youtube`, ...)
  - provider_user_id (text; e.g., Google sub or YouTube channel/user id)
  - status (`linked|pending|error|revoked`)
  - scopes (text/json), metadata (json)
  - created_at, updated_at
  - unique (user_id, provider)

## Auth and DB naming alignment

Existing schema (from migrations/schema.sql):

- users
  - user_id (PK), github_id (UNIQUE), email (NOT NULL UNIQUE), created_at, updated_at
- api_keys
  - key_hash (PK), user_id (FK → users.user_id), created_at, last_used, salt, iterations, lookup_hash
- jobs
  - job_id (PK), user_id (FK), status CHECK in (`pending`,`processing`,`completed`,`failed`,`cancelled`), progress (JSON string), drive_folder, extensions (JSON array string), created_at, completed_at, error
- google_tokens
  - user_id (PK, FK), access_token, refresh_token, expiry, token_type, scopes, created_at, updated_at

Planned additions (non-breaking):

- linked_accounts
  - id (uuid PK), user_id (FK), provider, provider_user_id, status, scopes, metadata, created_at, updated_at, UNIQUE(user_id, provider)
- jobs (new columns)
  - job_type (TEXT), source_url (TEXT), idempotency_key (TEXT), options (TEXT), output_refs (TEXT JSON)
- job_events
  - id (uuid PK), job_id (FK), type, message, data (JSON), created_at

## Security

- Keep JWT cookie with `HttpOnly`, `Secure`, `SameSite=Lax` (already implemented via settings)
- Add CSRF protection for form posts:
  - Generate per-session CSRF token; embed via Jinja macro as hidden field or meta tag
  - Configure HTMX to send the token header (`hx-headers` or a global script)
- Rate limiting:
  - Basic per-IP and per-user limits on `POST /jobs`
- CORS:
  - Only if external origins consume JSON API directly
- Headers:
  - HSTS, CSP (nonce or strict), X-Frame-Options, X-Content-Type-Options
- Idempotency:
  - Accept `Idempotency-Key` on `POST /jobs` to prevent duplicate enqueues

## Background Pipeline

- `POST /jobs`:
  - Validate/normalize Drive URL(s)
  - Create job(s) with `pending` status and idempotency key
  - Publish to Cloudflare Queue
- Worker:
  - Update `job_events` and `jobs.status` as work progresses
  - Store outputs/paths in `jobs.output_refs`
- UI:
  - Poll job status via HTMX partials

## Templates and Components

- `base.html` with navbar, user info, flash area, CSRF meta
- Components (macros): forms, tables, alerts, pagination
- Jobs pages with partials for rows/status to optimize HTMX updates
- Errors (404/500) rendered via Jinja

## Observability

- Structured logs with request IDs and user_id (already logging in API)
- Metrics: request latency, enqueue success/failure, worker durations
- Tracing (optional): OpenTelemetry for API and worker
- Audit log (later): admin actions recorded to DB

## Deployment

- Environments: dev/staging/prod via env vars in `api/config.py`
- Static assets: serve via FastAPI behind Cloudflare; optionally move to Pages/R2 later
- Database: managed Postgres (current DB layer compatible)
- Migrations: continue using `migrations/schema.sql` or introduce Alembic if needed
- Health: `/health` readiness

## Keep Current Auth

- GitHub OAuth endpoints already implemented in `api/main.py`
- JWT verification in `api/middleware.py` (plus API Key support)
- Google OAuth utilities exist (`api/google_oauth.py`) for optional “Link Google Drive” feature later; add routes when needed

## Extensibility: YouTube (future)

- Add YouTube as a provider in `linked_accounts` with placeholder routes now (`/auth/youtube/start|callback`).
- When implementing, store provider tokens/metadata analogous to `google_tokens` (separate table if needed).
- Introduce new `job_type='transcribe_video'` with its own enqueue path and worker handler.
- Reuse dashboard and jobs list UI with filters by `job_type`.

## Incremental Tasks (Execution Roadmap)

1) Templates and skeleton
- Add templates directory with `base.html`, `auth/login.html`, `jobs/dashboard.html`, partials
- Add `web.py` router for HTML routes and mount in app
- Include HTMX script and Tailwind CSS build

2) Jobs flows
- Create `POST /jobs` HTML handler returning HTMX partials
- Create job list/detail pages with polling partials
- Implement idempotency keys for submissions

3) Security and UX polish
- CSRF tokens wired into forms and HTMX headers
- Flash messages, error pages
- Rate limiting on submissions

4) Optional: Google account linking
- Add `/auth/google/start` and `/auth/google/callback` using `api/google_oauth.py`
- Show “Link Google Drive” button on dashboard if not linked

5) Admin/RBAC (later)
- Introduce roles and protected routes for admin views
- Audit log and API key management UI

## Acceptance Criteria (MVP)

- Users can sign in via GitHub (existing flow) and land on `/`
- Users can submit one or more Drive URLs and see job creation feedback without full page reload (HTMX)
- Users can view job history and live-updating status
- CSRF protection enabled for form posts
- Basic rate limiting on job submissions

---

Last updated: plan authored to align with existing GitHub OAuth and middleware; designed to scale into a richer CMS without changing core authentication.

## Option A Implementation (separate PR)

Scope: Add a server-rendered web UI with Jinja2 + HTMX using existing GitHub OAuth and middleware.

Deliverables:
- **Routes**
  - GET `/login` (links to `/auth/github/start`)
  - GET `/` dashboard (auth-gated)
  - POST `/jobs` (create/enqueue; HTMX partial response)
  - GET `/jobs`, GET `/jobs/{id}` (detail + status partial)
  - GET `/settings`, GET `/settings/accounts` (linked accounts UI)
  - GET `/auth/google/start`, GET `/auth/google/callback` (link Google)
- **Templates**
  - base.html, components/{form,table,alert}.html
  - auth/login.html
  - jobs/{dashboard,list,detail}.html
  - jobs/partials/{row,status_badge}.html
  - errors/{404,500}.html
- **Security**
  - CSRF token generation/validation for HTML posts
  - Idempotency for `POST /jobs` via header
- **Dev toggles**
  - Local worker stub to simulate queue processing
  - Env-driven toggle to use in-memory queue in dev

Non-goals in this PR:
- YouTube provider OAuth and transcription pipeline (placeholders only)
- Admin/RBAC UI (follow-up PR)
