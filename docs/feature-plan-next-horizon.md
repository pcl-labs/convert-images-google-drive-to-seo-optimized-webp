# Feature Plan — Next Horizon

This roadmap captures the next wave of work now that Phase 3 (blog generation pipeline + document versions) is in place.

## 1. Deployment & Ops (Cloudflare first)
- **Observability**: ship structured logs + metrics to Workers Analytics or Grafana; add Sentry (or Workers Trace Events) for pipeline failures.
- **PII retention**: cron job for `step_invocations`, `document_exports`, and temporary assets—documented SLAs.
- **Rollout checklist**: staging namespace, health checks, autoscaling limits.

Note: Deployed on Cloudflare; queue producer implemented with bindings configured. End-to-end queue processing is under validation.


You’re working in the repo `convert-image-webp-optimizer-google-drive`. Complete the following tasks end-to-end:

1. **Cloudflare Queue Integration** — ✅ Completed
   - End-to-end routing, retry/backoff + DLQ, deterministic ingest testing, and inline/Cloudflare guardrails are now covered by code + tests (`tests/test_youtube_ingest.py`, `tests/test_config_queue.py`). Logs link to `docs/DEPLOYMENT.md` and `.env`/`.env.example` explain the required secrets.
   - Follow-up: keep monitoring `wrangler tail` during deploys to ensure Workers Analytics/Sentry hooks stay healthy, but no more engineering work is blocking this area.

2. **API Docs + Logging**
   - Add logging around queue send failures with actionable error messages, including instructions pointing to `docs/DEPLOYMENT.md`.

3. **General Cleanup**
   - Ensure refs to `settings.queue` or `ensure_services()` handle inline queue objects cleanly.
   - Run `pytest` to verify tests pass with inline mode.


### 1.a Operationalize YouTube ingest & queue reliability — ✅ Wrapped
- [x] Queue flow validation: `tests/test_youtube_ingest.py::test_ingest_youtube_queue_flow` covers enqueue → consume without Cloudflare bindings.
- [x] Retry/backoff + DLQ: `jobs` table now tracks `attempt_count`/`next_attempt_at`, worker re-enqueues with exponential backoff, and final failures call `QueueProducer.send_to_dlq`.
- [x] Actionable logging & docs: enqueue failures link to `docs/DEPLOYMENT.md` and `.env` templates spell out the Wrangler secrets (`CF_ACCOUNT_ID`, `CF_API_TOKEN`, etc.).
- [x] Deterministic ingest tests: fast path mocks Google calls while the real API test remains opt-in.
- [x] Inline vs Cloudflare guardrails: `test_config_queue.py` asserts we skip half-configured clients; enqueue warnings remind devs to run the inline consumer in dev.

> ✅ Nothing blocking here—move on to Drive source-of-truth work.

-### 1.b Drive source-of-truth loop
- **✅ Drive workspace provisioning parity (new)**: YouTube ingest now creates a Drive workspace (folder + media folder) locally and in Workers. `metadata.drive` is stamped with folder/file IDs, last-ingested timestamps, and external-edit flags so the dashboard UI can mirror Drive. Drive overview still only counts `source_type=drive*` docs, so we’ll extend it once Docs ingestion lands.
- **Document ↔ Google Doc mapping** *(next)*:
  - API: add a `drive.docs` helper that either reuses `drive_file_id` if it exists or creates a Docs file inside the document workspace folder, returns file + revision metadata, and persists the IDs on the document row.
  - When a YouTube ingest finishes, call this helper (if enabled) and stream a `drive.workspace.doc` pipeline event so the timeline shows when the Doc was created.
  - Seed the Docs body with the raw transcript so Drive instantly holds the “raw text” artifact; persist the doc + revision IDs so Quill treats that file as canonical (no extra “doc export” step later).
- **Outline generation API before UI polish**: expose `/api/v1/pipelines/generate_outline` that runs just the outline step against `raw_text`, stores the outline in `metadata.latest_outline`, and emits `outline.generate` pipeline events so the editor can request outlines later. UI can follow once this endpoint is stable.
  - The outline step should also call Docs batchUpdate to strip the transcript block from the Drive file and replace it with the generated outline so the Doc stays accurate even before the UI refreshes.
- **Docs push-sync (webhook)**:
  - Register Google Drive push notifications (channel/watch) per Drive workspace using a Cloudflare Worker endpoint as the webhook target and persist `channelId`, `resourceId`, and expiration per user/document.
  - Worker webhook handler (Workers `POST /drive/webhook`) validates the channel secret, checks channel/resource IDs, and enqueues a `drive_change_poll` job with the change IDs so edits in Docs arrive within seconds.
  - Watches expire within ~24h, so add a renewal cron that re-registers watches before expiry and tears them down when a user disconnects Drive. Persist the renewal timestamp/token on each linked doc.
  - Keep a low-frequency cron (startPageToken poll) as a safety net whenever push notifications fail or a webhook delivery is skipped.
  - SLA: push latency median <30s / P95 <120s. If the webhook is degraded we must ensure the polling fallback keeps max lag <5m per document/user.
  - Polling fallback: run a staggered per-user check at least every 5 minutes, with exponential backoff + jitter when Drive returns 429/5xx, and enforce a token-bucket per user so we stay below Drive quotas while still touching every document within the SLA window.
  - Stale webhook detection: track `X-Goog-Message-Number` and heartbeat timestamps; if a channel is silent for >2 minutes or we detect skipped message numbers, auto-trigger polling and flag the channel for renewal.
  - Channel creation guardrails: limit renewals to 1/min/user and, when Drive reports subscription quota errors, degrade gracefully by prioritizing recently edited docs, queuing retries with backoff, and keeping polling active until quotas reset.
  - Webhook integrity: require HMAC-SHA256 signatures via `DRIVE_WEBHOOK_SECRET` (no secret → fail fast), reject invalid signatures, dedupe on `(channelId, resourceId, messageNumber)`, and make the job enqueue path idempotent so replayed notifications are ignored.
  - Ops checklist: set `DRIVE_WEBHOOK_URL` + `DRIVE_WEBHOOK_SECRET`, expose the Worker route publicly, and schedule `/api/v1/drive/watch/renew` (Cloudflare Cron or background worker) so channels are renewed automatically.
- **Editing + publish pipeline**: When the editor (or future AI agent) edits a document, send those diffs back to the Drive file via the Docs batchUpdate API, then mark a “ready for publish” version row so exports can pick it up. Store edit provenance in `document_versions` for auditing.
- **Drive change detection**: Add a worker/cron that polls Drive change IDs for linked docs, enqueues a lightweight “doc sync” job, and annotates the corresponding document/version when an external edit happens. This keeps the queue busy even if no YouTube ingest is running.
- **Docs-as-output**: Drive Docs now exist as soon as ingest runs, so every downstream export/update should patch that same file ID (outline, marketing brief, chapter rewrites) instead of creating duplicates. The API orchestrates ingestion, generation, and publishing while Drive remains the single source of truth.


## 2. Editor & Pipeline Polish
- **Milestone: Drive-linked document UI** *(in progress)*:
  - ✅ Card/grid redesign + detail header now surface Drive metadata (folder/doc links, last ingest timestamp, external-edit warnings).
  - 🚧 Next: surface Drive status in the dashboard overview, add manual “Sync Drive” controls, and auto-refresh metadata when new pipeline events arrive so the editor always mirrors the workspace.
  - Once the outline API exists, render the latest outline in a simple card (same style as current cards) and add a “Regenerate outline” button wired to the new endpoint; no UI overhaul required yet.
- **Bidirectional Drive edits** *(functional priority)*:
  - When webhook-driven sync detects a new revision, immediately pull the Doc body, update `raw_text`, and create a `document_versions` snapshot so Quill stays aligned with Google Docs.
  - When Quill pushes edits (generate outline, convert raw transcript to chapters, regenerate sections), update the Google Doc via Docs batchUpdate—replace the transcript with the outline when the user presses “Generate outline,” then push subsequent edits section-by-section. Record the revision ID on each save so both sides stay in lockstep.
- **Live notifications & activity stream**:
  - Promote `notifications_stream` to the primary channel for drive + pipeline updates: every `pipeline_event` (ingest, sync, outline regenerate, webhook-detected edit) should emit a structured notification/event row so the dashboard “Activity” view and toasts stay fresh without polling.
  - When the Drive webhook enqueues `drive_change_poll`, also enqueue a `notification.created` SSE payload that shows “Drive edit detected → syncing now,” then replace it with a success/failure notification when the ingest job finishes. Tie notifications to `document_id` so clicking them opens the document detail.
  - Hook job lifecycle events (queued → processing → completed) into the SSE feed so the UI can show “live” progress bars for ingest, outline, export, etc., using the same stream already exposed at `/notifications/stream`.
  - Document the `/api/stream` SSE endpoint in ops runbooks so dashboards can stay connected, and ensure Nginx/Workers keep connections open (no buffering, `X-Accel-Buffering: no` is already set).
- **Composable steps in UI**: surface outline/chapters regenerate options that call the `/api/v1/steps/*` endpoints individually so product teams (or agents) can tweak only a portion of a document without re-running the entire pipeline.
- **Inline diffing**: show how a regenerated section differs from the previous version (simple Markdown diff) before committing it as a new version row.
- **Image workflow**: allow users to choose which generated image prompt to keep, upload their own replacements, or re-run image generation per section.
- **AI configuration**: add per-user provider preferences (OpenAI, Cloudflare Workers AI, Anthropic) and surface token costs per section in the editor.

## 3. Export Connectors
- **Google Docs**: worker that consumes `document_exports` rows with `target = google_docs`, updates the ingest-created Drive doc (same file ID) instead of spawning a new file, and persists export status.
- **WordPress**: REST integration (JWT or app password) that posts to `/wp-json/wp/v2/posts`, mapping frontmatter to title/slug/meta. Store remote post IDs for updates.
- **Zapier / Webhook**: generic HTTPS POST with frontmatter + HTML so users can fan out to Notion, CMS, Slack, etc. Provide signing secrets + retry logic.
- **Status tracking UI**: show export history per document with timestamps, remote links, and retry controls.

## 4. Usage Metering & Billing
- **Stripe metered billing**: introduce `usage_aggregate` table + daily cron that reports usage to Stripe (or deducts credits). Block API/UI actions when quota exceeded.
- **In-product usage dashboard**: charts of tokens/steps per day, per document, and by model provider.
- **Plan management**: pricing tiers, upgrade/downgrade flows, coupon support.

## 5. API Hardening & SDK
- **API keys scoped to projects**: allow multiple keys per user, with RBAC for teammates.
- **Client SDK**: publish a tiny Python/JS SDK that wraps `/api/v1/documents/*`, `/api/v1/pipelines/generate_blog`, etc., including retries.
- **Streaming job events**: SSE or WebSocket channel for job progress so external dashboards can subscribe.

## 6. Content Quality Improvements
- **LLM prompt revamp**: split prompts per industry, add tone presets, leverage retrieval (Drive docs, transcripts) for factual grounding.
- **Human-in-the-loop**: add review states, assignments, and per-section approval before exports can run.
- **Data retention for fine-tuning**: store sanitized corpora (opt-in) for future proprietary models or embeddings.

Each theme can be pulled into its own PR.

Priority note: Cloudflare deployment & ops is next (queue integration, bindings, observability). Export connectors and billing will follow immediately after (some work can proceed in parallel once the queue path is in place).
