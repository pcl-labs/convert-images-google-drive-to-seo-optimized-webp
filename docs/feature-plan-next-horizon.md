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

You’re working in the repo `convert-image-webp-optimizer-google-drive`. Complete the following tasks end-to-end:

1. **Cloudflare Queue Integration**
   - Update settings/config to read Cloudflare queue bindings from env vars. Expect:
     - `CF_QUEUE_NAME`, `CF_ACCOUNT_ID`, `CF_API_TOKEN`, `CF_D1_BINDING` (if needed), plus bindings for DLQ if applicable.
   - In `api/config.py`, add these configs and ensure they flow into `QueueProducer`.
   - In local dev (`settings.environment != 'production'`), allow an optional “inline” queue (e.g., uses asyncio queue). Add a toggle env `USE_INLINE_QUEUE=true` so devs can run the worker without Cloudflare. In production, **require** real bindings—raise a clear error at startup if queue missing.
   - Update `cloudflare_queue.py` to:
     - Use the Cloudflare API (Queue send endpoint) when bindings are set.
     - Provide an inline queue fallback when `USE_INLINE_QUEUE` is true (just store message in an asyncio queue).
     - Add docstrings/comments describing how to configure the Cloudflare CLI vs. dashboard.

2. **Worker Entry Point**
   - Create a CLI/runner (e.g., `python workers/consumer.py --inline`) that:
     - Uses inline queue if `USE_INLINE_QUEUE` is set, otherwise connects to Workers Queue API (pull loop or rely on Cloudflare worker?). Clarify dev instructions in README.
   - Add instructions to `docs/DEPLOYMENT.md` for:
     - Creating the queue via Cloudflare CLI (`wrangler queues create ...`).
     - Setting env vars locally (`CF_ACCOUNT_ID`, `CF_API_TOKEN`, `CF_QUEUE_URL` etc.).
     - Wiring queue + DLQ in the Workers dashboard.

3. **Env Files**
   - Update `.env.example` (and note in docs) with new vars:
     ```
     USE_INLINE_QUEUE=true
     CF_ACCOUNT_ID=
     CF_API_TOKEN=
     CF_QUEUE_NAME=
     CF_QUEUE_DLQ=
     ```
   - Document in README what each env var does and the security requirements for API token scopes.

4. **API Docs + Logging**
   - In `api/public.py` root response, include a flag showing whether queue is inline vs. Cloudflare (helps debugging).
   - Add logging around queue send failures with actionable error messages, including instructions pointing to `docs/DEPLOYMENT.md`.

5. **General Cleanup**
   - Ensure refs to `settings.queue` or `ensure_services()` handle inline queue objects cleanly.
   - Run `pytest` to verify tests pass with inline mode.

Provide references in your commit summary for files touched. Also note any manual steps I need to do in the Cloudflare dashboard (e.g., create queue named `quill-jobs`, add API token with Queue Write and D1 read/write scopes) in `docs/feature-plan-next-horizon.md` or `docs/DEPLOYMENT.md`.

Env var recap / where to place them

Add to .env.example and mention in README:


USE_INLINE_QUEUE=true                # default for local dev
CF_ACCOUNT_ID=                       # Cloudflare account ID
CF_API_TOKEN=                        # token with Workers Queue write + D1 permissions
CF_QUEUE_NAME=quill-jobs             # primary queue
CF_QUEUE_DLQ=quill-dlq               # optional DLQ
CF_QUEUE_URL=https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/queues/$CF_QUEUE_NAME/messages
For production (Cloudflare Workers):

Set USE_INLINE_QUEUE=false.
Provide CF_ACCOUNT_ID, CF_API_TOKEN (Queue:write, Workers Scripts:read, D1:read/write), CF_QUEUE_NAME, CF_QUEUE_DLQ.
Configure bindings in the Cloudflare dashboard or via wrangler.toml—document the exact steps in docs/DEPLOYMENT.md.
You’ll also need to update wrangler.toml (if you have one) with queue bindings, e.g.:


[[queues.producers]]
 binding = "JOB_QUEUE"
 queue = "quill-jobs"

[[queues.consumers]]
 queue = "quill-jobs"
 script = "worker_consumer"
…but spell out the final instructions in the docs.

Once Cursor finishes the coding work, double-check README/docs mention:

How to run USE_INLINE_QUEUE=true python workers/consumer.py.
How to configure Cloudflare queue + API token.

## 6. Content Quality Improvements
- **LLM prompt revamp**: split prompts per industry, add tone presets, leverage retrieval (Drive docs, transcripts) for factual grounding.
- **Human-in-the-loop**: add review states, assignments, and per-section approval before exports can run.
- **Data retention for fine-tuning**: store sanitized corpora (opt-in) for future proprietary models or embeddings.

Each theme can be pulled into its own PR.

Priority note: Cloudflare deployment & ops is next (queue integration, bindings, observability). Export connectors and billing will follow immediately after (some work can proceed in parallel once the queue path is in place).
