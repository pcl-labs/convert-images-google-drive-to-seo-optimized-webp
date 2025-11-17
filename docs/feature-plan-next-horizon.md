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

### 1.b Drive source-of-truth loop
- **✅ Drive workspace provisioning parity (new)**: YouTube ingest now creates a Drive workspace (folder + media folder) locally and in Workers. `metadata.drive` is stamped with folder/file IDs, last-ingested timestamps, and external-edit flags so the dashboard UI can mirror Drive. Drive overview still only counts `source_type=drive*` docs, so we’ll extend it once Docs ingestion lands.
- **Document ↔ Google Doc mapping** *(next)*: extend the ingest path to either reuse or create a Docs file (via Docs API) and treat that file’s revision as the canonical store. Persist `drive_file_id`, `revision_id`, and `last_ingested_revision` on the document row.
- **Editing + publish pipeline**: When the editor (or future AI agent) edits a document, send those diffs back to the Drive file via the Docs batchUpdate API, then mark a “ready for publish” version row so exports can pick it up. Store edit provenance in `document_versions` for auditing.
- **Drive change detection**: Add a worker/cron that polls Drive change IDs for linked docs, enqueues a lightweight “doc sync” job, and annotates the corresponding document/version when an external edit happens. This keeps the queue busy even if no YouTube ingest is running.
- **Docs-as-output**: When export jobs target Drive, reuse the same file ID to update specific ranges (e.g., marketing brief, image slots) instead of creating a new file. This keeps the Drive doc authoritative while the API orchestrates ingestion, generation, and publishing.

(Keep the existing Sections 2–6 as-is; they follow once Section 1 subsections land.)

## 2. Editor & Pipeline Polish
- **Milestone: Drive-linked document UI** *(in progress)*:
  - ✅ Card/grid redesign + detail header now surface Drive metadata (folder/doc links, last ingest timestamp, external-edit warnings).
  - 🚧 Next: surface Drive status in the dashboard overview, add manual “Sync Drive” controls, and auto-refresh metadata when new pipeline events arrive so the editor always mirrors the workspace.
- **Composable steps in UI**: surface outline/chapters regenerate options that call the `/api/v1/steps/*` endpoints individually so product teams (or agents) can tweak only a portion of a document without re-running the entire pipeline.
- **Inline diffing**: show how a regenerated section differs from the previous version (simple Markdown diff) before committing it as a new version row.
- **Image workflow**: allow users to choose which generated image prompt to keep, upload their own replacements, or re-run image generation per section.
- **AI configuration**: add per-user provider preferences (OpenAI, Cloudflare Workers AI, Anthropic) and surface token costs per section in the editor.

## 3. Export Connectors
- **Google Docs**: worker that consumes `document_exports` rows with `target = google_docs`, creates/updates a Drive doc, and persists export status.
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
