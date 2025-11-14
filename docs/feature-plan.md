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

     * `document(id, user_id, source_type, source_ref, raw_text, media_refs, transcription_status, ...)`

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

## 2. What to build next, in order

### Phase 1 — Normalize “content” and unify job types

**Goal:** Everything becomes “content-in → document”.

You already have:

* `jobs` table, Cloudflare Queue, Drive optimization logic
* You want: YouTube/text/uploads → blog generation

**Step 1.1: Add core content tables**

Migrations (D1 is fine for now):

* `documents`

  * `document_id`
  * `user_id`
  * `source_type` (`"youtube" | "drive" | "text" | "upload"`)
  * `source_ref` (youtube video id, drive folder id, file id, etc.)
  * `raw_text` (text from paste or transcript)
  * `metadata` (json: title, language, duration, etc.)
  * `created_at`, `updated_at`

* Optional but nice:

  * `document_media` if you want to track images/audio separately.

**Step 1.2: Extend `jobs` to support pipelines**

You already planned migrations, so:

* Add:

  * `job_type TEXT NOT NULL`
  * `document_id` (nullable — some jobs are “ingest”, some “generate”)
  * `output JSON` (for final blog, or you can reuse `progress` for now)

Examples:

* Ingest job:

  * `job_type="ingest_youtube"`, creates a `document` + sets `document_id`
* Blog generation job:

  * `job_type="generate_blog"`, references an existing `document_id`

**Step 1.3: Implement ingestion for multiple sources**

You already have:

* YouTube job idea
* Drive integration
* Simple text forms

Implement three ingestion paths (all create a `document` then a `job`):

1. **YouTube ingestion**

   * `POST /ingest/youtube`
   * Input: YouTube URL
   * Flow:

     * Extract video id, create `document(source_type="youtube", source_ref=video_id)`
     * Enqueue job `job_type="ingest_youtube"` with that `document_id`

2. **Text ingestion**

   * `POST /ingest/text`
   * Input: raw text textarea
   * Flow:

     * Create `document(source_type="text", raw_text=...)`
     * Optionally immediately create `generate_blog` job, or let user click “Generate blog” later.

3. **Drive ingestion (images)**

   * You already have:

     * Drive folder + optimization job
   * Adapt that path so it also:

     * Creates a `document(source_type="drive", source_ref=folder_id)`
     * Stores metadata about image files (optional now, crucial later for images in blog)

Front-end wise, your “input content” card becomes:

* Tabs: Text | YouTube | Drive

---

### Phase 2 — Plug in your transcriber + build the generation pipeline

**Goal:** Take a `document` and produce a structured blog.

You said you already have Python transcriber/code; so we’re mainly about wiring & splitting into steps.

**Step 2.1: Port transcriber as a reusable module**

Create something like:

* `core/transcription.py`

  * `transcribe_youtube(video_id) -> text + metadata`
  * `transcribe_audio_file(file_path) -> text + metadata`

Wire it into the `ingest_youtube` job:

* Worker: `process_ingest_youtube_job(document_id, job_id, user_id)`

  * Get YouTube URL/ID from `document`
  * Call transcriber
  * Update `document.raw_text` and `document.metadata` (title, duration, etc.)
  * Mark job as `completed`

**Step 2.2: Define the blog-generation pipeline as steps**

Decide on minimal steps:

1. `outline` – from `document.raw_text` → list of chapters
2. `chapters` – generate each chapter body
3. `seo` – title, description, slug, tags
4. `images` – 1 image spec per chapter (can initially just be alt-text & a “download image from Drive” mapping)

Represent each step as a **pipeline_step** row:

* `pipeline_runs`

  * `run_id`, `document_id`, `user_id`, `status`, `created_at`
* `pipeline_steps`

  * `step_id`, `run_id`, `step_type` (`outline`, `chapter`, `seo`, `images`)
  * `status`, `input`, `output`, `tokens_in`, `tokens_out`, `started_at`, `completed_at`

For now, these can just be JSON and run sequentially inside **one job**; you don’t *have* to fan them out as separate jobs yet.

**Step 2.3: Implement AI modules as small functions**

In e.g. `core/generation.py`:

* `run_outline_step(document_text) -> outline_struct`
* `run_chapters_step(document_text, outline) -> [chapter_struct]`
* `run_seo_step(document_text, outline) -> seo_struct`
* `run_images_step(chapters) -> [image_struct]`

Each of these:

* Calls your model API
* Returns structured JSON
* Emits a `usage_events` row (Phase 3)

Worker function for `generate_blog` job:

* Fetch `document.raw_text`
* Orchestrate steps in order
* Save orchestrated results into:

  * `pipeline_runs` / `pipeline_steps`
  * And/or `jobs.output` with a final assembled blog

---

### Phase 3 — Usage metering so you can bill

**Goal:** Track exactly “how much AI” every user consumes.

You don’t need bills yet; you need **usage events**.

**Step 3.1: Usage tables**

Migrations:

* `usage_events`

  * `id`
  * `user_id`
  * `job_id`
  * `run_id` (optional, for pipeline run)
  * `step_type` (`transcribe`, `outline`, `chapter`, `seo`, `images`, etc.)
  * `model`
  * `tokens_in`
  * `tokens_out`
  * `created_at`

Later:

* `usage_aggregates`

  * `user_id`
  * `date`
  * `tokens_in`, `tokens_out`
  * `cost_estimate`

**Step 3.2: Hook it into AI calls (and transcriber if relevant)**

Wherever you call the model, wrap with:

```python
result, tokens_in, tokens_out = call_model(...)
record_usage_event(user_id, job_id, run_id, "outline", tokens_in, tokens_out)
```

Do this for:

* Transcription (if it’s AI-based token usage)
* Outline
* Each chapter
* SEO
* Images (if you use a model for alt text or image prompts)

**Step 3.3: Expose usage to the user**

API/UI:

* `GET /usage` → returns totals per day/month
* Simple dashboard card: “Tokens used this billing period”

You use that later for Stripe metering.

---

### Phase 4 — Billing & plans (Stripe-style)

**Goal:** Make it commercially viable without over-engineering.

**Step 4.1: User → Customer mapping**

* Add columns on `users`:

  * `stripe_customer_id`
  * `plan` (enum/free/pro)
* Or have a separate `billing_customers` table.

**Step 4.2: Plan & quota model**

Simplest viable:

* Plans:

  * Free: X tokens/month
  * Pro: Y tokens/month
* Derived from `usage_aggregates`.

Enforce:

* On each job or each usage event, if `tokens_this_period + new_tokens > quota`:

  * Reject or queue as “paused due to quota”.

**Step 4.3: Stripe integration (when you’re ready)**

You can:

* Use Stripe subscriptions for recurring plans.
* Use Stripe’s **metered billing** or your own quotas (Stripe just charges base subscription; you charge variable based on usage events aggregated and reported to Stripe).

This is more of an infra/product call; but your groundwork is:

* `usage_events` + `usage_aggregates` per user
* Quota enforcement hooks
* Stripe webhooks to update plan / status

---

## 3. TL;DR — Build order tailored to where you are

Given that you’ve already done a bunch of tightening:

**Next 1–2 sessions (backend focus):**

1. **Add content & job_type schema**

   * `documents` table.
   * Extend `jobs` with `job_type`, `document_id`, and an `output` JSON column.

2. **Implement ingestion jobs**

   * `ingest_youtube`: create document + job, use your transcriber, save transcript into `documents.raw_text`.
   * `ingest_text`: create document with pasted text.

3. **Add `generate_blog` job & simple pipeline**

   * Orchestrate: outline → chapters → SEO in a single worker function, storing results in `jobs.output`.

**After that:**

4. **Introduce `pipeline_runs` + `pipeline_steps`** to break blog generation into traceable steps.

5. **Add `usage_events`** and wrap all model calls/transcriber in “record usage” helpers.

6. **Expose usage via a small API/endpoint + UI card.**

7. **Then** start Stripe plan/quota work when you’re ready.

---

## Phase 1 — PR summary (implemented)

- **Schema**
  - Added `documents` table (id, user_id, source_type, source_ref, raw_text, metadata JSON, created/updated).
  - Extended `jobs` with `job_type`, `document_id`, `output` (JSON TEXT) + indexes.
  - For dev SQLite, schema ensured idempotently at startup; for D1, added to `migrations/schema.sql`.

- **DAL & Models**
  - Documents CRUD helpers; extended job creation (`create_job_extended`).
  - `JobType` enum and extended `JobStatus` to include `job_type`, `document_id`, `output`.

- **API**
  - New ingestion endpoints: `POST /ingest/text`, `POST /ingest/youtube`.
  - Updated `POST /api/v1/optimize` to also create a Drive `document` and return `job_type`/`document_id`.

- **Worker**
  - Queue message routing by `job_type` with Phase 1 stubs for ingestion jobs.

- **UI**
  - Dashboard uses `job_type` to derive job kind label.

- **Tests**
  - Added docs CRUD and ingestion endpoint tests; adjusted existing tests to new behaviors.

## Testing notes and philosophy (what we fixed/learned)

- **API root vs HTML root**
  - The HTML root (`/`) renders a page; the JSON API info is at `/api`. Tests updated to hit `/api` for JSON.

- **OAuth start endpoints**
  - `GET /auth/google/start` performs a redirect (e.g., 302/307) rather than requiring prior auth; tests should expect redirect, not 401.

- **Queue dependency in tests**
  - Endpoints that enqueue jobs use `ensure_services()`; unit tests should provide a mock queue:
    - Set a `Database` via `set_db_instance(...)`.
    - Provide a `QueueProducer` with a mocked `queue.send` via `set_queue_producer(...)`.

- **Dev DB migrations**
  - SQLite dev uses idempotent startup migration to keep tests hermetic and fast; D1 uses `migrations/schema.sql`.

- **Consistent response fields**
  - Job responses now include `job_type` and `document_id`; tests assert these fields for ingestion/optimize endpoints.

This section can be used directly in the Phase 1 PR description.

---

## Phase 2 — Current status (in progress)

- Implemented transcript pipeline scaffolding:
  - youtube-transcript-api captions first, fallback to yt-dlp audio + faster-whisper (CPU, `small.en`).
  - Helpers: `core/transcripts.py`, `core/audio_fetch.py` (temp dirs, timeout, error handling, cleanup).
  - Config flags in `api/config.py` with validation and parsing:
    - `ENABLE_YTDLP_AUDIO`, `ASR_ENGINE`, `WHISPER_MODEL_SIZE`, `ASR_DEVICE`, `ASR_MAX_DURATION_MIN`, `TRANSCRIPT_LANGS`.
  - Usage metering: `usage_events` table + `record_usage_event(...)` helper.
  - Worker integration: `process_ingest_youtube_job` now fetches transcript, updates `documents.raw_text/metadata`, records usage, and sets job output.

### Learnings and guardrails

- PII-safe logging in queue/worker (do not log whole messages).
- Temp files and dirs must be cleaned; use context managers and explicit cleanup for moved files.
- Config values should be validated (enums) and parsed (comma-separated lists) at load time.
- Add CHECK/FK constraints to metering tables to avoid invalid data and orphans.
- Provide local test knobs (e.g., `TEST_YOUTUBE_VIDEO_ID`, `TEST_HTTP_TIMEOUT`).

## Phase 2 — Remaining work to complete

- Tests
  - Unit tests for `core/transcripts.py` and `core/audio_fetch.py` (mock yt-dlp and faster-whisper).
  - Worker integration test for `ingest_youtube` no-captions path (ensures document update and usage events).

- API and UI for usage
  - Minimal endpoints to fetch per-user usage: `/api/v1/usage/summary`, `/api/v1/usage/events`.
  - Dashboard card to show minutes processed and MB downloaded for the current period.

- Pipeline progression (generate_blog)
  - Define minimal outline/chapters/seo steps and persist results (`jobs.output` initially; later `pipeline_runs`/`pipeline_steps`).
  - Record usage events per step (tokens in/out) once model calls are added.

- Operational controls
  - Add `ASR_MAX_DURATION_MIN` enforcement in worker before transcription.
  - Feature flag to disable ASR entirely (captions-only mode) for constrained envs.
  - Retries/backoff lanes for network-bound steps (yt-dlp, captions fetch).

- Docs
  - README note on FFmpeg prerequisite for local dev.
  - Link usage endpoints and environment flags.

### Nice-to-have (later in Phase 2 or early Phase 3)

- Aggregated usage view (daily/monthly rollups) and quota checks.
- Optional external ASR provider integration behind feature flag.