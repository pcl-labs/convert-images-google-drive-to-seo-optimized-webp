# Feature Plan — Next Horizon

This roadmap tracks the outstanding work now that Phase 3 (blog generation pipeline + document versions) exists.

## Recently Completed
- ✅ Cloudflare queue + retry/backoff path hardened and documented.
- ✅ Drive doc provisioning, outline step, and dashboard controls (Sync Drive + Generate/Regenerate outline) are live end-to-end.

## 1. Deployment & Ops (Cloudflare first)
- **Observability**: ship structured logs + metrics to Workers Analytics/Grafana and add Sentry (or Workers Trace Events) for pipeline failures.
- **PII retention**: cron job for `step_invocations`, `document_exports`, and temporary assets with published SLAs.
- **Rollout checklist**: staging namespace, health checks, autoscaling limits.
- **API Docs + Logging**: add logging around queue send failures with actionable remediation steps (linking to `docs/DEPLOYMENT.md`).
- **General cleanup**: ensure `settings.queue`/`ensure_services()` handle inline queue objects cleanly and keep `pytest` runs green in inline mode.

### Drive source-of-truth loop
- **Docs push-sync (webhook)**: implement Drive channel registration, webhook validation, renewal cron, polling fallback, stale-channel detection, quota guardrails, and HMAC verification. Expose `/drive/webhook` publicly and schedule `/api/v1/drive/watch/renew`.
- **Editing + publish pipeline**: when Quill edits occur (outline, regenerate chapters, compose blog), push diffs to Docs via batchUpdate and mark a “ready for publish” version snapshot.
- **Drive change detection**: poll Drive change IDs per document, enqueue lightweight “doc sync” jobs, and annotate versions when an external edit happens so UI and Drive stay aligned.
- **Docs-as-output**: ensure every export/update patches the existing Drive file (no duplicates) by reusing the stored `drive_file_id`/revision info.

## 2. Editor & Pipeline Polish
- **Drive-linked document UI** ✅ Cards show Drive metadata, status badges, Sync Drive, and outline Generate/Regenerate controls directly on the document detail page.
- **Bidirectional Drive edits (TODO)**:
  - Webhook-driven revisions should immediately update `raw_text`, create a `document_versions` snapshot, and raise user-facing notifications.
  - Quill-originated edits (outline, chapter regeneration, AI compose) must push to Docs section-by-section, recording revision IDs on each save.
- **Live notifications & activity stream**:
  - Promote `notifications_stream` to the primary channel for drive + pipeline updates (ingest, sync, outline regenerate, webhook-detected edit).
  - When Drive webhook enqueues `drive_change_poll`, send a “Drive edit detected → syncing now” SSE payload; replace it with success/failure when the job finishes.
  - Hook job lifecycle events (queued → processing → completed) into SSE so progress bars stay live.
  - Document `/api/stream` SSE expectations (disable buffering, `X-Accel-Buffering: no`).
- **Composable steps in UI**: expose outline/chapters regenerate options that hit `/api/v1/steps/*` individually so agents can tweak portions without rerunning the entire pipeline.
- **Inline diffing**: display how regenerated sections differ from previous revisions before committing them to `document_versions`.
- **Image workflow**: allow selecting/generated image prompts per section, uploading replacements, or re-running image generation.
- **AI configuration**: per-user provider preferences (OpenAI, Workers AI, Anthropic) with token cost surfacing per section.

## 3. Export Connectors
- **Google Docs**: worker consumes `document_exports` rows with `target = google_docs`, updates the ingest-created Drive doc, and tracks export status.
- **WordPress**: REST integration (JWT or app password) that posts to `/wp-json/wp/v2/posts`, mapping frontmatter to title/slug/meta and storing remote IDs.
- **Zapier / Webhook**: generic HTTPS POST with frontmatter + HTML plus signing secrets/retry logic.
- **Status tracking UI**: show export history per document with timestamps, remote links, and retry controls.

## 4. Usage Metering & Billing
- **Stripe metered billing**: introduce `usage_aggregate` + daily cron reporting usage to Stripe (or deducting credits); block actions when quota exceeded.
- **Usage dashboard**: charts of tokens/steps per day, per document, and by model provider.
- **Plan management**: pricing tiers, upgrade/downgrade flows, coupon support.

## 5. API Hardening & SDK
- **Project-scoped API keys**: multiple keys per user with RBAC for teammates.
- **Client SDK**: publish Python/JS SDK wrapping `/api/v1/documents/*`, `/api/v1/pipelines/generate_blog`, etc., with retries.
- **Streaming job events**: SSE or WebSocket channel for job progress so external dashboards can subscribe.

## 6. Content Quality Improvements
- **LLM prompt revamp**: industry-specific prompts, tone presets, retrieval grounding (Drive docs, transcripts).
- **Human-in-the-loop**: review states, assignments, per-section approval before exports run.
- **Data retention for fine-tuning**: store sanitized corpora (opt-in) for proprietary models/embeddings.

## 7. YouTube → Blog Autopilot
1. **Better outline + chapters**: replace the naive outline with a structured hierarchy. Pull YouTube chapter markers when available, keep full-sentence summaries, reserve CTA/keyword slots, and render both JSON + Drive-friendly text so AI receives consistent structure.
2. **Automated pipeline**: submitting a YouTube URL should enqueue ingest → outline → chapter shaping → AI compose → Drive sync automatically, emitting pipeline events/SSE so users watch a todo list flip green without manual steps.
3. **Final handoff**: when the AI draft lands, send a “Blog ready” notification (with Google Doc link) and surface a status chip in the documents grid (“Ready”, “Needs review”, etc.) so the UX becomes “paste URL, get SEO-ready Doc.”
