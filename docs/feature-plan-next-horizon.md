# Feature Plan — Next Horizon

Roadmap ordered by the sequence we need to ship to make “Paste YouTube URL → get an SEO-ready Google Doc” a reality.

## 0. Cleanup & Hardening (prep before planner)
- ✅ Removed the legacy outline HTMX endpoints (`/dashboard/documents/{id}/outline/regenerate`, `/api/v1/steps/*`) so everything flows through `/v1/content/*`.
- Normalize document metadata: ensure `latest_outline`, `latest_sections`, and upcoming `metadata.content_plan` stay in sync after autopilot runs (one source of truth, Drive mirrors the same structure).
- Purge stale template fragments and JS that referenced manual “Generate outline” flows; convert remaining Alpine/HTMX hooks to the new job-link pattern.
- Add regression tests for `/v1/content/blog_from_*` happy path (ingest + compose) plus error handling so cleanup doesn’t regress the API.

## Recently Completed
- ✅ Cloudflare queue + retry/backoff path hardened and documented.
- ✅ Drive doc provisioning, outline step, and dashboard controls (Sync Drive + Generate/Regenerate outline) are live.

## 1. Immediate Priorities — Autopilot Flow
- ✅ **Structured outlines + chapters**
  - ✅ Replace the naive outline with a real heading/summary hierarchy (intro, body, CTA slots, keywords) and persist it into `metadata.latest_outline`.
  - ✅ Pull YouTube chapter markers when available and merge them into `latest_outline` + Drive rendering.
- ✅ **Automatic pipeline execution**
  - ✅ Submitting a YouTube URL now enqueues ingest → outline → chapter shaping → AI compose → Drive sync automatically, and `/v1/content/blog_from_*` exposes the same flow for API clients.
  - ✅ Emit pipeline events/SSE so users watch a todo list flip green (dashboard panels link directly to `/api/pipelines/stream?job_id=...`).
- ✅ **Final handoff & UX**
  - ✅ When the AI draft lands, documents grid + detail cards show the latest status, and Job/API links are surfaced inline so “Open Google Doc” is the primary CTA.
  - ⏭️ TODO: send a “Blog ready” notification email/webhook once Drive sync confirms the ai_draft stage.

## 2. Drive Source-of-Truth & Sync
- **Docs push-sync (webhook)**: implement Drive watches, webhook validation, renewal cron, polling fallback, stale-channel detection, quota guardrails, and HMAC verification. Expose `/drive/webhook` and schedule `/api/v1/drive/watch/renew`.
- **Bidirectional edits**:
  - Webhook-driven revisions must refresh `raw_text`, create a `document_versions` snapshot, and notify the user.
  - Quill-originated edits (outline, chapter regeneration, AI compose) should batchUpdate Docs section-by-section, recording revision IDs.
- **Drive change detection**: poll Drive change IDs, enqueue lightweight “doc sync” jobs, and annotate versions when external edits happen.
- **Docs-as-output**: ensure every export/update patches the existing Drive file (no duplicates) by reusing stored `drive_file_id`/revision info.

## 3. Editor & Pipeline Polish
- **Content API polish (new)**:
  - Document `/v1/content/blog_from_{youtube,text,document}` parameters, response schemas, and async behaviors in Dev Docs / ReDoc so external teams can self-serve.
  - Add smoke/integration tests that hit the FastAPI app end-to-end to ensure dashboard + API stay in lockstep as we add new schema types (FAQPage, HowTo, etc.).
- **Live notifications & activity stream**:
  - Promote `notifications_stream` to the primary channel for drive + pipeline updates (ingest, sync, outline regenerate, webhook-driven edit).
  - When Drive webhook enqueues `drive_change_poll`, send “Drive edit detected → syncing now” SSE payloads and replace them on completion/failure.
  - Hook job lifecycle events (queued → processing → completed) into SSE; document `/api/stream` buffering requirements.
- **Composable steps in UI**: expose outline/chapters regenerate actions that hit `/api/v1/steps/*` so agents can tweak sections without rerunning the entire pipeline.
- **Inline diffing**: show regenerated section diffs before committing to `document_versions`.
- **Image workflow**: allow choosing/generated prompts per section, uploading replacements, or re-running image generation.
- **AI configuration**: per-user provider preferences (OpenAI, Workers AI, Anthropic) with token cost surfacing per section.

## 4. Export Connectors
- **Google Docs**: worker consumes `document_exports` rows with `target = google_docs`, updates the ingest-created Drive doc, and tracks export status.
- **WordPress**: REST integration (JWT/app password) that posts to `/wp-json/wp/v2/posts`, mapping frontmatter to title/slug/meta and storing remote IDs.
- **Zapier / Webhook**: general HTTPS POST with frontmatter + HTML plus signing secrets/retries.
- **Status tracking UI**: show export history per document with timestamps, remote links, and retry controls.

## 5. Usage Metering & Billing
- **Stripe metered billing**: add `usage_aggregate` + daily cron reporting usage to Stripe (or deducting credits); block actions when quota exceeded.
- **Usage dashboard**: charts of tokens/steps per day, per document, and by model provider.
- **Plan management**: pricing tiers, upgrade/downgrade flows, coupon support.

## 6. API Hardening & SDK
- **Content API cleanup (new)**:
  - Split the public `/v1` OpenAPI tags from internal `/api/v1` routes, ensure every field (tone, content_type, instructions, mode/format) has defaults + enum docs, and publish reference examples.
  - Provide copy/paste snippets (curl + Python) showing synchronous vs async usage with job polling and SSE streams.
- **Project-scoped API keys**: multiple keys per user with RBAC for teammates.
- **Client SDK**: publish Python/JS SDK wrapping `/api/v1/documents/*`, `/api/v1/pipelines/generate_blog`, etc., with retries.
- **Streaming job events**: SSE or WebSocket channel for job progress so external dashboards can subscribe.

## 7. Content Quality Improvements
- **LLM prompt revamp**: industry-specific prompts, tone presets, retrieval grounding (Drive docs, transcripts).
- **Human-in-the-loop**: review states, assignments, per-section approval before exports.
- **Data retention for fine-tuning**: store sanitized corpora (opt-in) for proprietary models/embeddings.

## 8. Deployment & Ops (Cloudflare)
- **Observability**: ship structured logs + metrics to Workers Analytics/Grafana and add Sentry (or Workers Trace Events) for pipeline failures.
- **PII retention**: cron job for `step_invocations`, `document_exports`, and temporary assets with published SLAs.
- **Rollout checklist**: staging namespace, health checks, autoscaling limits.
- **API Docs + Logging**: add logging around queue send failures with actionable remediation steps (linking to `docs/DEPLOYMENT.md`).
- **General cleanup**: ensure `settings.queue`/`ensure_services()` handle inline queue objects cleanly and keep `pytest` runs green in inline mode.
