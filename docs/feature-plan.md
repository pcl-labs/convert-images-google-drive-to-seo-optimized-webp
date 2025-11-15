Right, cool—so we’re not “optimize some images” anymore, we’re building:

> A content-ingestion + AI blog-writing engine with usage-based billing.

You already have:

* Auth (GitHub), users, API keys
* Jobs + Cloudflare Queue + worker
* Drive integration + image optimizer
* Your own Python transcriber code you can port in

So the question is: **what do we build next, and in what order, to turn this into a blog-writing SaaS?**

I’ll give you:

1. A **high-level architecture** (components, not code)
2. A **concrete build order** (Phase 1 → Phase 4)

---

## 1. High-level architecture (what this thing wants to become)

Think in 5 layers:

1. **Content Sources & Ingestion**

   * Inputs:

     * Raw text (paste)
     * File uploads / Drive folders (images, docs, audio, video)
     * YouTube URLs
   * Normalizes everything into a **Document** model:

     * `document(id, user_id, source_type, source_ref, raw_text, media_refs, transcription_status, content_format, frontmatter, structured_output, ...)`
     * Store both source payloads (transcripts/raw text) and the **rendered output contract**:
       * `content_format`: `mdx`, `markdown`, `html`, etc.
       * `frontmatter`: JSON blob mirroring YAML frontmatter (title, slug, seo, hero image refs).
       * `structured_output`: normalized JSON (outline, sections, assets) so we can export to any channel later.
       * `versions`: optional pointer to future `document_versions` table for revision history/export diffs.

2. **Job & Pipeline Orchestration**

   * A job is now **one run of a pipeline**:

     * `job_type` examples:

       * `ingest_youtube`
       * `ingest_drive_folder`
       * `ingest_text`
       * `generate_blog_from_document`
   * Each job runs as background work via:

     * Cloudflare Queue → Worker → your `workers/consumer.py`
   * Pipelines have **steps**:

     * Transcribe
     * Outline
     * Chapters
     * SEO metadata
     * Images per chapter
     * Final assembly

3. **AI Orchestration Layer**

   * A small library of “modules” (like your mental codex-IDE):

     * `run_outline_prompt(document_text) -> outline`
     * `run_chapter_prompt(outline, chapter_idx) -> chapter text`
     * `run_seo_prompt(document_text, outline) -> title, description, slug, keywords`
     * `run_image_prompt(chapter_text) -> image spec / URL`
   * Each module:

     * Takes a clear input payload
     * Calls the model
     * Returns structured output
     * **Emits a usage event** with tokens & step metadata

4. **Usage Metering & Billing**

   * Low-level events:

     * `usage_events(id, user_id, job_id, step_type, tokens_in, tokens_out, model, created_at)`
   * Aggregates:

     * Daily/interval buckets per user:

       * `usage_aggregate(user_id, date, tokens_in, tokens_out, cost_estimate)`
   * Billing integration:

     * Stripe customer + subscription
     * Either:

       * Stripe metered billing via usage reports, or
       * Your own “credits” table on top of Stripe.

5. **API & UI**

   * API endpoints:

     * Content ingestion (create/update documents)
     * Start blog-generation jobs
     * Check job status & fetch outputs
     * Get usage metrics
   * UI (what you’re doing today):

     * Dashboard to create jobs & view status
     * Editor-like view of generated blog
     * Integrations page
     * Usage/billing page

---



---

## Phase 2.5 — Step-Oriented API/Tools (agent-friendly)

- Rationale
  - Make steps composable and testable; enable AI agents to call granular tools.
  - Keep a convenience pipeline endpoint that orchestrates the same steps.

- Proposed step endpoints (all POST)
  - /api/v1/steps/transcript.fetch
    - input: video_id, langs[]
    - output: {text, lang, duration_s, source}
    - metering: event_type=transcribe, engine=captions, duration_s
  - /api/v1/steps/outline.generate
    - input: text|doc_id, options
    - output: {outline: [...]}
    - metering: tokens_in/out, model
  - /api/v1/steps/chapters.organize
    - input: text|doc_id
    - output: {chapters: [{title, summary, start_s?}]}
    - metering: tokens_in/out, model
  - /api/v1/steps/blog.compose
    - input: outline|chapters, tone/length
    - output: {markdown|html, meta}
    - metering: tokens_in/out, model
  - /api/v1/steps/document.persist
    - input: doc_id, fields
    - output: {doc_id, version}
    - metering: persist

- Design guardrails
  - Idempotency keys for all POSTs.
  - Consistent job envelope for async steps: {job_id}; GET /api/v1/jobs/{job_id}.
  - PII-safe, structured logging; no raw text in logs.
  - Strict validation; bounded Query/Body; masked 5xx errors.
- **UI:** Dashboard now has a Documents page that calls these endpoints (Drive import, YouTube ingest, text paste), and the job form consumes Document IDs directly. Editor/usage views remain todo.

---

## Phase 3 — Generate Blog pipeline

- Scope
  - Orchestrate: outline → chapters → SEO → compose.
  - Persist to jobs.output (JSON) initially.
  - Add a convenience POST /api/v1/pipelines/generate_blog that invokes the above steps.
  - Pipeline output contract:
    - `content_format`: default `mdx`.
    - `frontmatter`: `{ title, slug, description, tags, hero_image, timestamps }`.
    - `body.mdx`: the canonical MDX (chapters + AI generated assets).
    - `body.html`: cached HTML rendering for previews.
    - `sections`: array with chapter metadata, timestamps for video clips, image prompts, alt text.
    - `assets`: references to optimized images + transcripts (Drive file IDs, CDN URLs).

- LLM integration and metering
  - Provider config flags (API key, model, timeouts).
  - Record usage events with tokens_in, tokens_out, model, latency_ms.

- Endpoints
  - POST /api/v1/steps/* as above; plus pipeline orchestrator endpoint.
  - GET /api/v1/jobs/{job_id} to fetch status/results.

- Tests
  - Unit tests for each step (mock LLM).
  - Pipeline integration test (mock LLM), usage metering assertions.

- Ops
  - Retries/backoff for model calls; step timeouts.
  - Observability: structured logs with job_id, step, duration_ms.
  - **Cloudflare TODO:** once the dashboard is updated, wire the real Workers Queue + bindings (we're still running locally).
  - **LLM/Training data plan:** decide how to spend Cloudflare Workers AI credits vs. external providers, and persist sanitized document/usage data (e.g., in D1/R2) for future fine-tuning.
  - Idempotency cache retention (future PR): schedule periodic cleanup of `step_invocations` (e.g., delete rows older than 24–48h) to limit storage/PII exposure. Ensure index on `(user_id, request_hash)` for fast duplicate detection. Service layer must sanitize or allowlist `response_body` fields to avoid PII; consider redaction/encryption if needed.

---

## Phase 3.5 — Content Packaging & Export Foundation

- Document output shape
  - Persist a normalized `document_versions` table: `{document_id, version, content_format, frontmatter_json, mdx_body, html_body, outline_json, assets_manifest, created_at}`.
  - Each `generate_blog` job writes a new version row (immutability) and updates `documents.latest_version_id`.
  - Support lightweight diff metadata (e.g., `source_job_id`, `source_step_ids`) for traceability and audit trails.

- Copy/export surfaces
  - **Copy as MDX**: Dashboard detail view exposes one-click copy of the exact MDX (frontmatter + body) and provides download as `.mdx`.
  - **Copy as Markdown/HTML**: derived from the same version row, so editors can drop into Notion/GDocs quickly.
  - **Export targets (queued for future PRs)**:
    - Google Docs: use Drive API to create/update doc with frontmatter metadata.
    - Zapier webhook: POST `frontmatter + html` bundle to partner workflows.
    - WordPress: REST API integration to publish draft posts with featured images + SEO fields.
    - CMS webhook: generic `POST /api/v1/hooks/export` for user-registered endpoints.
  - Design export API so each connector consumes the same normalized payload: `frontmatter`, `mdx`, `html`, `assets`.
  - Include `export_status` + `export_history` tables to track success/failure per target and re-play jobs.

- Editor roadmap alignment
  - Editor UI should read from `document_versions` and allow toggling between `outline`, `MDX`, and `Rendered` tabs.
  - Provide “Regenerate section” actions that spawn partial jobs while preserving earlier versions.
  - Inline asset picker (images/transcript snippets) references the `assets_manifest`.

- Developer ergonomics
  - Guardrails: strict schema for version payload (Pydantic model) + JSON schema file stored in repo for contract tests.
  - Tests: assert that generating pipeline produces valid MDX (YAML frontmatter + markdown) and that exports deserialize correctly.
