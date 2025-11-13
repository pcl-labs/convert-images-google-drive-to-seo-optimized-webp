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
- Optional later: GET `/settings`, POST `/auth/logout` (client-side cookie clear if needed; current flow uses JWT cookie)

HTMX patterns:
- `hx-post` for form submission, `hx-target` pointing to the list container
- `hx-swap="outerHTML"` to replace updated rows/sections
- `hx-trigger="every 2s"` on a status partial for polling

## Data Model (extensible)

Existing tables for users, api_keys, jobs, google_tokens appear to be in place. Proposed additions or confirmations:

- users (existing via GitHub OAuth)
  - id (user_id), github_id, email, role (optional)
- sessions (optional server-side sessions; current design uses JWT cookie)
- jobs (existing)
  - id, user_id, source_url, status, output_refs (json), idempotency_key, options (json), timestamps
- job_events (recommended)
  - id, job_id, type, message, data (json), created_at
- api_tokens (existing)
  - rotation and scopes later
- google_tokens (existing)
  - used if/when enabling Google account linking

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
  - Create job(s) with `queued` status and idempotency key
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
