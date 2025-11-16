# Feature Plan — Next Horizon

This roadmap captures the next wave of work now that Phase 3 (blog generation pipeline + document versions) is in place.

## 1. Deployment & Ops (Cloudflare first)
- **Observability**: ship structured logs + metrics to Workers Analytics or Grafana; add Sentry (or Workers Trace Events) for pipeline failures.
- **PII retention**: cron job for `step_invocations`, `document_exports`, and temporary assets—documented SLAs.
- **Rollout checklist**: staging namespace, health checks, autoscaling limits.

Note: Deployed on Cloudflare; queue producer implemented with bindings configured. End-to-end queue processing is under validation.


You’re working in the repo `convert-image-webp-optimizer-google-drive`. Complete the following tasks end-to-end:

1. **Cloudflare Queue Integration**
   - a) End-to-end queue message routing validated from producer to consumer.
     - Blocks release: Yes
     - Verification: Integration test sends a job through `QueueProducer` and verifies consumption and processing by the worker; log trace confirms enqueue → receive → handle. CI job must pass.
   - b) Retry and backoff behavior verified under failure conditions.
     - Blocks release: Yes
     - Verification: Simulate consumer failure; confirm retries up to configured `max_job_retries` with backoff intervals, and final DLQ placement when applicable. Logs and metrics reflect attempts and outcome.
   - c) Metrics and alerting emitted for queue failures.
     - Blocks release: No (required before production rollout)
     - Verification: Structured logs for failures present; Workers Analytics (or Grafana) metrics increment on enqueue failure and DLQ send; alert configured for error rate threshold as per `docs/DEPLOYMENT.md`.
   - d) API client initialization guards in place (no malformed URLs, DLQ optional).
     - Blocks release: Yes
     - Verification: Unit tests assert `cloudflare_account_id` is required when `use_inline_queue=false`, and that both main/DLQ API clients are skipped if missing. Tests in `tests/test_config_queue.py` must pass in CI.

2. **API Docs + Logging**
   - Add logging around queue send failures with actionable error messages, including instructions pointing to `docs/DEPLOYMENT.md`.

3. **General Cleanup**
   - Ensure refs to `settings.queue` or `ensure_services()` handle inline queue objects cleanly.
   - Run `pytest` to verify tests pass with inline mode.


## 1. Operationalize YouTube ingest & queue reliability
- **Queue flow validation (blocker)**: Build an integration test that instantiates `QueueProducer` with a stub transport, enqueues an `ingest_youtube` job via `start_ingest_youtube_job`, and then hands the serialized message to `workers.consumer.handle_queue_message` to assert the document receives metadata + `raw_text`. This proves enqueue → consume works without hitting Cloudflare (`tests/test_youtube_ingest.py` is the place to extend). CI must run it with inline mode enabled.
- **Retry/backoff + DLQ (blocker)**: Add `attempt_count`/`next_attempt_at` columns to `jobs`, teach the worker to re-enqueue up to `settings.max_job_retries`, and have final failures call `QueueProducer.send_to_dlq` with structured metadata. Write a regression test that forces `process_ingest_youtube_job` to raise, then asserts retries, timestamps, and DLQ placement are recorded.
- **Actionable logging & docs (blocker)**: When `enqueue_job_with_guard` or `CloudflareQueueAPI.send` fails, emit logs that explain how to fix bindings/secrets and link directly to `docs/DEPLOYMENT.md` (include the exact Wrangler commands). In the same doc, add the missing `wrangler secret put CLOUDFLARE_ACCOUNT_ID|CLOUDFLARE_API_TOKEN|CF_QUEUE_NAME|CF_QUEUE_DLQ` steps plus a reminder to set `USE_INLINE_QUEUE=false` in production.
- **Deterministic ingest tests (blocker)**: Add a fast path in `tests/test_youtube_ingest.py` that mocks `build_youtube_service_for_user`, `fetch_video_metadata`, and `fetch_captions_text` so we always assert `raw_text`, transcript metadata, and job output shape without needing real OAuth tokens. Keep the “real API” test opt-in for manual smoke tests.
- **Inline vs Cloudflare guardrails (blocker)**: Extend `test_config_queue.py` to cover the case where `QueueProducer` is built with missing DLQ bindings to ensure we never try to use half-configured Cloudflare clients; surface a warning in `api/utils.enqueue_job_with_guard` whenever we silently fall back to inline mode so operators know they still need to run `python workers/consumer.py --inline`.

## 2. Drive source-of-truth loop
- **Document ↔ Google Doc mapping**: Extend `documents` metadata to track the Drive file ID + revision so we can treat a Google Doc as the canonical store. Add an ingestion step (queue job) that fetches the doc body via Drive/Docs API, normalizes it into `raw_text`, and updates `document_versions`.
- **Editing + publish pipeline**: When the editor (or future AI agent) edits a document, send those diffs back to the Drive file via the Docs batchUpdate API, then mark a “ready for publish” version row so exports can pick it up. Store edit provenance in `document_versions` for auditing.
- **Drive change detection**: Add a worker/cron that polls Drive change IDs for linked docs, enqueues a lightweight “doc sync” job, and annotates the corresponding document/version when an external edit happens. This keeps the queue busy even if no YouTube ingest is running.
- **Docs-as-output**: When export jobs target Drive, reuse the same file ID to update specific ranges (e.g., marketing brief, image slots) instead of creating a new file. This keeps the Drive doc authoritative while the API orchestrates ingestion, generation, and publishing.

(Keep the existing Sections 3–6 as-is; they still follow once Sections 1–2 land.)

Let me know if you’d like any tweaks to the wording or additional milestones before I start editing the file.


## 2. Editor & Pipeline Polish
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
