# Feature Plan — Next Horizon

This roadmap captures the next wave of work now that Phase 3 (blog generation pipeline + document versions) is in place.

## 1. Deployment & Ops (Cloudflare first)
- **Cloudflare Workers Queue**: switch the queue producer/worker to real bindings, add dead-letter handling, and alarms for stuck jobs.
- **Observability**: ship structured logs + metrics to Workers Analytics or Grafana; add Sentry (or Workers Trace Events) for pipeline failures.
- **PII retention**: cron job for `step_invocations`, `document_exports`, and temporary assets—documented SLAs.
- **Rollout checklist**: staging namespace, health checks, autoscaling limits.

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

Each theme can be pulled into its own PR. The immediate next PR should focus on export connectors + billing, since those unlock end-to-end value for customers.
