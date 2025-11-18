# Content API Design Plan

## 1. Goal and Scope

This document specifies a developer-friendly **Content API** for generating blog articles and similar content from different sources (YouTube, raw text, etc.), with:

- Clear, consistent resource paths.
- Explicit control over **output shape** (structured vs markdown) and **wire format** (JSON, MDX, HTML).
- Well-named knobs for style/tone and extra **instructions**.
- A clean mapping to existing FastAPI models, jobs, and document/version storage.

This plan now serves as the spec for the implemented `/v1/content`, `/v1/documents`, and `/v1/jobs` routes plus the dashboard wiring that consumes them.

---

## 2. High-Level API Shape

Base prefix for the new endpoints:

- `POST /v1/content/...`
- Supporting resources under:
  - `/v1/documents/...`
  - `/v1/jobs/...`

### 2.1 Core Concepts

- **Content source**: where we get the raw material
  - YouTube URL
  - Raw text
  - (Future: Drive docs, web URLs, etc.)

- **Output mode** (semantic shape):
  - `mode = "structured"` → strongly-typed content object (outline, sections, metadata).
  - `mode = "markdown"` → rendered article body (MDX or HTML).

- **Wire format** (transport encoding):
  - `format = "json" | "mdx" | "html"`.

- **Control knobs**:
  - `tone`, `temperature`, `model`, `max_sections`, `target_chapters`, `include_images`, etc.
  - `instructions` → free-form steering text ("extra directions"), e.g. "Don’t mention homeless people shown in the video".

- **Execution model**:
  - `async = false` → synchronous generation, returns content directly.
  - `async = true` → returns a `JobStatus`, client later fetches result.

### 2.2 Naming and Resource Boundaries

- **Public generation namespace**: `content`
  - Used for operations that *generate* or transform material, e.g.:
    - `POST /v1/content/blog_from_youtube`
    - `POST /v1/content/blog_from_text`
  - This keeps room for future operations like `summary_from_document`, `social_from_document`, etc.

- **Stored resources namespace**: `documents`
  - Used for persisted artifacts (ingested sources and generated outputs):
    - `GET /v1/documents/{document_id}`
    - `GET /v1/documents/{document_id}/versions/{version_id}`
  - Mirrors existing `Document` and `DocumentVersion*` models and keeps internal terminology intact.

- **Jobs namespace**: `jobs`
  - Used for long-running operations and polling:
    - `GET /v1/jobs/{job_id}`
  - Thin wrapper over existing job status APIs, but aligned with the `/v1` surface.

---

## 3. Current Capabilities and Mapping

### 3.1 Existing Models

From `src/workers/api/models.py`:

- **Ingestion**
  - `IngestYouTubeRequest`
    - `url: HttpUrl`
  - `IngestTextRequest`
    - `text: str`
    - `title: Optional[str]`

- **Blog generation**
  - `GenerateBlogRequest`
    - `document_id: str`
    - `options: GenerateBlogOptions`
  - `GenerateBlogOptions`
    - `tone: Optional[str]`
    - `max_sections: Optional[int]`
    - `target_chapters: Optional[int]`
    - `include_images: Optional[bool]`
    - `model: Optional[str]`
    - `temperature: Optional[float]`
    - `section_index: Optional[int]`

- **Documents and versions**
  - `Document`
  - `DocumentVersionSummary`
  - `DocumentVersionDetail`
    - `body_mdx`, `body_html`, `outline`, `chapters`, `sections`, `assets`, `frontmatter`, etc.

- **Jobs**
  - `JobStatus`, `JobStatusEnum`, `JobType`.

### 3.2 Existing API Endpoints

From `src/workers/api/protected.py` and `web.py`:

- Ingestion and pipelines:
  - `start_ingest_youtube_job` (internal helper)
  - `start_ingest_text_job`
  - `start_generate_blog_job` (uses `GenerateBlogRequest`)
  - `POST /api/v1/pipelines/generate_blog` → returns `JobStatus`.

- Documents and versions:
  - `GET /api/v1/documents/{document_id}/versions`
  - `GET /api/v1/documents/{document_id}/versions/{version_id}` → `DocumentVersionDetail`.
  - `GET /api/v1/documents/{document_id}/versions/{version_id}/body?format=mdx|html`
    - Returns MDX or HTML body as text.

- Jobs:
  - `GET /api/v1/jobs/{job_id}` → `JobStatus` with optional `output`.

### 3.3 Mapping to Planned API

The planned `/v1/content` endpoints are thin orchestration on top of existing pieces:

- **Source handling**:
  - YouTube: reuse `IngestYouTubeRequest` + `start_ingest_youtube_job`.
  - Text: reuse `IngestTextRequest` + `start_ingest_text_job`.

- **Blog generation**:
  - Reuse `GenerateBlogRequest` + `GenerateBlogOptions` + `start_generate_blog_job`.

- **Content retrieval**:
  - Reuse `DocumentVersionDetail` and `/body` endpoints for `mode`/`format` variants.

- **Async vs sync**:
  - Async: reuse `JobStatus` + `/api/v1/jobs/{job_id}`.
  - Sync: internally wait for job completion (or run in inline mode) and then fetch the document version, returning it in desired shape.

No new core data models are strictly required; this plan mostly introduces **new public endpoints** and **schemas that wrap existing models** into a nicer contract.

### 3.4 Migration and deprecation of legacy `/api/v1/...` routes

Historically, a number of endpoints were exposed under `/api/v1/...` (e.g. `/api/v1/documents/...`, `/api/v1/jobs/...`, `/api/v1/pipelines/generate_blog`). For the Content API, we are standardizing on the versioned `/v1/...` surface:

- **Canonical public API**
  - All new SDKs, docs, and integrations must use:
    - `/v1/content/...`
    - `/v1/documents/...`
    - `/v1/jobs/...`

- **Legacy `/api/v1/...` status**
  - This project is effectively greenfield for external consumers.
  - Legacy `/api/v1/...` endpoints that overlap with `/v1/...` will be **removed as part of the same rollout** (no long-term compatibility window is required).
  - The remaining `/api/...` routes are explicitly **internal**, e.g. the SSE stream at `/api/pipelines/stream`.

- **Behavioral differences**
  - **Auth**: both old and new endpoints rely on the same underlying auth (session cookies or API keys). `/v1/...` simply codifies API-key usage for headless consumers.
  - **Response shapes**: `/v1/...` responses are explicitly modeled and stable (see §4–§5). Legacy `/api/v1/...` responses may have been shaped primarily for the dashboard and are not considered stable.
  - **Headers & rate limits**: no intentional behavior change; existing middleware (CORS, rate limiting, security headers) continues to apply uniformly.

- **Migration strategy**
  - Recommended path: **use `/v1/...` exclusively** for any new integration (SDKs, headless sites, CLIs).
  - Because there are no known external consumers of `/api/v1/...`, we do not maintain a backwards-compatibility period; overlapping routes under `/api/v1/...` can be deleted once `/v1/...` is wired.
  - Internal UI has already been updated (or will be updated in the same PR) to call `/v1/...` for content/document/job operations.

- **SDKs & docs**
  - All public docs and code samples should reference `/v1/...` only.
  - SDK generators (OpenAPI-based) should target the `/v1/...` schema.
  - Any mention of `/api/v1/...` in docs should be either removed or explicitly labeled as **internal/legacy** and not for external use.

- **Migration checklist (internal)**
  - [ ] Replace dashboard calls to `/api/v1/documents/...` and `/api/v1/jobs/...` with the new `/v1/documents/...` and `/v1/jobs/...` wrappers.
  - [ ] Remove unused public references to `/api/v1/...` from docs and code comments.
  - [ ] Delete redundant `/api/v1/...` routes once tests for `/v1/...` pass.
  - [ ] Publish a short “Content API v1” migration note pointing here for details.

---

## 4. Proposed Public Endpoints

### 4.1 `POST /v1/content/blog_from_youtube`

Generate a blog article from a YouTube URL. The server automatically ingests the video, queues blog generation, and (when `async=false` and inline queues are enabled) returns the rendered plan + markdown in one response.

#### Request

```jsonc
{
  "youtube_url": "https://www.youtube.com/watch?v=VIDEO_ID",   // required

  "mode": "structured",        // "structured" | "markdown" (default: structured)
  "format": "json",            // "json" | "mdx" | "html" (default is mode-aware)

  "tone": "energetic",
  "max_sections": 6,
  "target_chapters": 4,
  "include_images": true,
  "model": "gpt-5.1",
  "temperature": 0.7,

  "content_type": "https://schema.org/BlogPosting",  // Schema.org identifier; defaults to BlogPosting
  "instructions": "Don’t reference the B-roll in the cold open.",

  "async": false
}
```

#### Responses

- **Structured JSON (`mode=structured`, `format=json`)** – status `200 OK`.

  Response schema (conceptual; see suggested Pydantic models below):

  ```jsonc
  {
    "document_id": "doc_123",            // string
    "version_id": "ver_004",             // string
    "created_at": "2025-11-18T01:02:03Z",// ISO8601
    "updated_at": "2025-11-18T02:03:04Z",// ISO8601
    "document_version": {
      "title": "How Bull City Legal Services Keeps Justice Affordable",
      "status": "published",             // draft | published | archived
      "author_id": "user_abc",           // optional
      "locale": "en-US",                 // optional
      "published_at": "2025-11-18T01:02:03Z", // optional
      "metadata": {                        // arbitrary JSON blob
        "site": "nc_legal",
        "source": "youtube"
      },
      "content_format": "blog",          // mirrors DocumentVersionDetail.content_format
      "frontmatter": {
        "slug": "how-bull-city-legal-works",
        "tags": ["legal", "durham"]
      }
    },
    "plan": {
      "schema": "blog.post",            // e.g. blog.post, faq.page
      "schema_version": 1,
      "content_type": "https://schema.org/BlogPosting",
      "intent": "educate",
      "audience": "founders and operators",
      "sections": [                      // array of Section
        {
          "id": "intro",               // stable identifier
          "heading": "Hook readers with a mission",
          "body": "## Hook readers...",// MDX fragment
          "order": 0,
          "purpose": "intro",          // intro | body | cta | etc.
          "key_points": [
            "Two-sentence mission statement",
            "Region served"
          ],
          "cta": false,
          "call_to_action": null
        }
      ],
      "cta": {                           // overall CTA block
        "text": "Ready to help?",       // human-readable CTA text
        "url": "https://nclegal.org/contact", // optional link
        "type": "primary"              // e.g. primary | secondary
      },
      "seo": {
        "title": "How Bull City Legal Services Keeps Justice Affordable",
        "description": "Inside the sliding-scale law firm helping the Triangle.",
        "keywords": ["legal aid", "durham", "nonprofit law firm"]
      },
      "outline": [                       // minimal outline entries
        { "id": "intro", "title": "Introduction" }
      ],
      "chapters": [                      // optional chapter-level groupings
        { "id": "ch1", "title": "Background" }
      ],
      "assets": [                        // media or external refs
        { "id": "thumb1", "type": "image", "ref": "gs://bucket/thumb.jpg" }
      ]
    }
  }
  ```

  Suggested Pydantic models for OpenAPI/SDK generation (names only):

  - `DocumentPlanResponse`
    - `document_id: str`
    - `version_id: str`
    - `created_at: datetime`
    - `updated_at: datetime`
    - `document_version: DocumentVersionDetailSubset`
    - `plan: Plan`
  - `DocumentVersionDetailSubset`
    - Subset of `DocumentVersionDetail` (title, status, author_id, locale, published_at, metadata, content_format, frontmatter).
  - `Plan`
    - `schema: str`
    - `schema_version: int`
    - `content_type: str`
    - `intent: Optional[str]`
    - `audience: Optional[str]`
    - `sections: list[Section]`
    - `cta: Optional[CTA]`
    - `seo: Optional[SEO]`
    - `outline: list[OutlineItem]`
    - `chapters: list[OutlineItem]`
    - `assets: list[Asset]`
  - `Section`
    - `id: str`
    - `heading: str`
    - `body: str`
    - `order: int`
    - `purpose: Optional[str]`
    - `key_points: list[str]`
    - `cta: bool`
    - `call_to_action: Optional[str]`
  - `CTA`
    - `text: str`
    - `url: Optional[str]`
    - `type: Optional[str]`
  - `SEO`
    - `title: str`
    - `description: str`
    - `keywords: list[str]`
  - `OutlineItem`
    - `id: str`
    - `title: str`
  - `Asset`
    - `id: str`
    - `type: str`
    - `ref: str`

- **Markdown (`mode=markdown`, `format=mdx|html`)** – status `200 OK`. Body is the rendered MDX/HTML fragment containing the article.
- **Async (`async=true` or non-inline queue)** – status `202 ACCEPTED` with:

```jsonc
{
  "job_id": "job_abc123",
  "job_type": "ingest_youtube",
  "status": "pending",
  "document_id": "doc_123",
  "mode": "structured",
  "format": "json",
  "detail": "Ingest + autopilot pipeline enqueued. Monitor /api/pipelines/stream for live updates (internal SSE endpoint)."
}
```

Clients can then poll `/v1/jobs/{job_id}` or subscribe to the internal SSE endpoint at `/api/pipelines/stream?job_id=job_abc123`.

### 4.2 `POST /v1/content/blog_from_text`

Same semantics as §4.1 but the source is raw text (`text` + optional `title`). The `content_type` and `instructions` fields behave identically. When async, the response describes the `generate_blog` job that was queued.

### 4.3 `POST /v1/content/blog_from_document`

Generate (or regenerate) a blog from an existing document ID.

```jsonc
{
  "document_id": "doc_123",
  "mode": "structured",
  "format": "json",
  "options": {
    "tone": "thoughtful",
    "content_type": "https://schema.org/FAQPage",
    "instructions": "Answer each question in < 160 words."
  },
  "async": true
}
```

If inline execution is available and `async=false`, the response mirrors the structured/markdown shapes from §4.1. Otherwise the response is `202 Accepted` with the `generate_blog` job metadata.

### 4.4 Documents APIs (WordPress-style)

`/v1/documents` is the primary resource for stored content, analogous to `wp-json/wp/v2/posts`:

- **List documents**

  ```http
  GET /v1/documents?site=nc_legal&status=published&per_page=10&page=1
  ```

  Supported query params (initial set):

  - `site` (string; optional):
    - Logical site key, e.g. `nc_legal`, used to group documents by consuming site or brand.
    - Backed by a field in `documents.metadata.site`.
  - `status` (string; optional):
    - `published` | `draft` | `any` (default: `published` for public/API-key clients).
    - Mirrors WordPress `status` (`publish`, `draft`, etc.).
  - `slug` (string; optional):
    - Matches `frontmatter.slug` and returns at most one document.
  - `per_page` (int; optional):
    - Page size (default 10, max 100).
  - `page` (int; optional):
    - Page number (1-based).

  Response (shape, not exact schema):

  ```jsonc
  [
    {
      "document_id": "doc_123",
      "slug": "how-bull-city-legal-works",
      "title": "How Bull City Legal Services Keeps Justice Affordable",
      "site": "nc_legal",
      "status": "published",              // see §5.2
      "published_at": "2025-11-18T01:02:03Z",
      "updated_at": "2025-11-18T02:03:04Z",
      "content_type": "https://schema.org/BlogPosting",
      "latest_version_id": "ver_004"
    }
  ]
  ```

- **Get a single document**

  ```http
  GET /v1/documents/{document_id}
  GET /v1/documents?site=nc_legal&slug=how-bull-city-legal-works
  ```

  Returns the document metadata plus (optionally) an embedded latest-version summary.

- **Version-aware APIs**

  - `GET /v1/documents/{document_id}/versions/{version_id}?mode=structured|markdown&format=json|mdx|html`
    - Fetch a specific version, either as JSON (`DocumentVersionDetail`) or as raw MDX/HTML.
  - `GET /v1/documents/{document_id}/versions/latest?mode=structured|markdown&format=json|mdx|html`
    - Convenience endpoint for the newest version.

  These correspond to WordPress revisions (`/posts/{id}/revisions`) but exposed as first-class endpoints.

- **Publish/unpublish APIs** (planned)

  - `POST /v1/documents/{document_id}/publish`
    - Marks the document as `status=published`, sets `published_at`, and pins `published_version_id` to the current version.
    - Optionally syncs associated Drive files into the published folder.
  - `POST /v1/documents/{document_id}/unpublish`
    - Marks the document as `status=draft`, clears `published_at` / `published_version_id`, and optionally moves files back to drafts.

These endpoints wrap the existing `documents` + `document_versions` tables and provide a WordPress-like surface for external sites: list by status/site and fetch by slug or ID.

> **Note:** Publish/unpublish operations are **future work** and are not part of the current `/v1/documents` surface. See §7 for planned follow-ups.

### 4.5 Jobs API

`GET /v1/jobs/{job_id}` returns:

```jsonc
{
  "job": {
    "job_id": "job_abc123",
    "job_type": "generate_blog",
    "status": "completed",
    "document_id": "doc_123",
    "progress": { "stage": "completed", ... },
    "output": { "document_id": "...", "version_id": "...", ... }
  },
  "links": {
    "document": "/v1/documents/doc_123",
    "latest_version": "/v1/documents/doc_123/versions/latest",
    "version": "/v1/documents/doc_123/versions/ver_004"
  }
}
```

Use this endpoint in combination with `/api/pipelines/stream?job_id=...` to provide instant progress and a REST-friendly status check.

### 4.6 Streaming / Live Updates

`GET /api/pipelines/stream?job_id=...` continues to emit Server-Sent Events for ingest + generation pipelines. This endpoint is intentionally **unversioned** and lives under `/api/...` as an internal streaming channel, while the public REST surface remains under `/v1/...`. UI surfaces (dashboard, document detail) link directly to this stream so users can watch the entire “Paste URL → Ready Doc” flow without manual outline steps.

### 4.7 Dashboard + UI integration

The FastAPI dashboard now talks to the Content API instead of older `/dashboard/*` helpers:

- The **YouTube ingest** card calls `POST /v1/content/blog_from_youtube` via Alpine.js. Responses render inline flash messages containing the job ID plus deep links to `/v1/jobs/{id}`, `/v1/documents/{id}`, and `/api/pipelines/stream?job_id=...`. The same panel automatically opens the SSE stream for that job so users can watch the end-to-end ingest + generation pipeline.
- The **Raw text** card submits to `POST /v1/content/blog_from_text` and reuses the same flash UI so schema cues (`content_type`, `instructions`) ride along with every request.
- The **Document detail → Generate blog** form now posts to `POST /v1/content/blog_from_document`. Successful responses update the `#doc-flash` region with the API links so users can open the new version directly or jump to the pipeline stream.
- Jobs cards (dashboard + per-document) show the job ID in monospace plus the REST/SSE shortcuts, mirroring the JSON `links` returned by `/v1/jobs/{id}`.

This keeps the dashboard UX and public API perfectly aligned—every “Generate” action exercises the same contract that third-party developers use, and the UI always exposes the job/document identifiers needed for API or SSE tooling.

---

## 5. Schema Additions and Naming

### 5.1 Generation fields

- `content_type`: String identifier (currently Schema.org IDs, defaulting to `https://schema.org/BlogPosting`). Additional types like `FAQPage`, `HowTo`, or `Recipe` can be layered on without breaking the API.
- `instructions`: Free-form steering text propagated to the planning/composition stages.

These fields are available on every `/v1/content/*` request and stored in `documents.metadata.latest_generation` for reuse.

- **Name**: `instructions`
- **Type**: `Optional[str]`
- **Max length**: up to 4,000 characters (requests exceeding this should be rejected with a 400 or truncated server-side with a warning).
- **Semantics**: Additional natural-language instructions steering the generation (constraints, emphasis, exclusions).
- **Examples**:
  - "Don’t mention the homeless people shown in the video."
  - "Keep it under 800 words and focus on practical steps."

Conflict resolution with other knobs:

- `tone`, `content_type`, and other structured options remain the primary source of truth.
- `instructions` may refine or narrow these settings but should *not* silently override them.
  - If `instructions` clearly contradicts `content_type` or `tone`, the planner should favor the structured options and log the conflict (and optionally return a 400 if the conflict is severe).

Persistence & filtering:

- `instructions` should be persisted in `documents.metadata.latest_generation.instructions` so that recent runs can be inspected.
- It is not intended as a primary filter dimension (e.g., no full-text search guarantees), but can be inspected for debugging and analytics.

Prompt pipeline integration:

- `instructions` is injected into **both** the planning and composition prompts:
  - Planning: to shape the ContentPlan (sections, CTA, SEO).
  - Composition: to steer phrasing, emphasis, and exclusions in the final MDX/HTML.

### 5.2 Document status and publishing

To mirror WordPress `status` and `date` semantics, each document and version should track explicit publication metadata:

- On `documents` (DB + Pydantic):
  - `status: Literal["draft", "published", "archived"]` (string enum)
  - `published_at: Optional[datetime]`
  - `published_version_id: Optional[str]`
  - `site: Optional[str]` (logical site key, e.g. `nc_legal`)

Semantics:

- `status="draft"` – content is internal/prepared; should not appear on public feeds.
- `status="published"` – content is live; appears in `/v1/documents?...&status=published` and is safe for headless sites.
- `status="archived"` – content is read-only or removed from feeds but still stored.
- `published_at` – first publish time, used for ordering and feeds.
- `published_version_id` – specific version that is considered “live” for the site, even if later drafts exist.
- `site` – lets multi-tenant consumers (e.g. northcarolinalegalservices.org) filter their own documents.

API behavior:

- List endpoints default to `status=published` when called by external API-key clients.
- Dashboard and internal tools can request `status=draft,published` to show everything.
- `/v1/documents/{id}` includes `status`, `published_at`, `published_version_id`, and `site` so headless consumers can make informed decisions.

### 5.3 Content plan schema

Every generation run now produces a structured plan stored at `document.metadata.content_plan` (and mirrored back in API responses via `plan`). The shape is:

```jsonc
{
  "schema": "blog.post",
  "schema_version": 1,
  "content_type": "https://schema.org/BlogPosting",
  "intent": "educate",
  "audience": "founders and operators",
  "sections": [
    {
      "order": 0,
      "slug": "hook-readers-with-a-mission",
      "title": "Hook readers with a mission",
      "summary": "Explain who the subject is, why the work matters, and set expectations.",
      "purpose": "intro",
      "key_points": [
        "Two-sentence mission statement",
        "Region served"
      ],
      "cta": false,
      "call_to_action": null
    },
    {
      "order": 4,
      "slug": "cta",
      "title": "Call to action",
      "summary": "Tie the benefits together and ask the reader to schedule a consult.",
      "purpose": "cta",
      "key_points": [
        "Sliding scale fees",
        "Virtual visits"
      ],
      "cta": true,
      "call_to_action": "Book a free consult"
    }
  ],
  "cta": {
    "summary": "Ready to help?",
    "action": "Schedule a consult"
  },
  "seo": {
    "title": "How Bull City Legal Services Keeps Justice Affordable",
    "description": "Inside the sliding-scale law firm helping the Triangle.",
    "keywords": [
      "legal aid",
      "durham",
      "nonprofit law firm"
    ]
  },
  "provider": "openai",
  "instructions": "Focus on sliding scale pricing and attorney culture."
}
```

- `sections` drive the UI, Drive mirrors, and downstream composition. Each entry exposes a slug, summary, purpose, and `key_points`.
- `outline`/`chapters` are still persisted for backwards compatibility, but they’re derived from the sections above so they stay in lockstep.
- `provider` lets us tell whether the structured planner ran (`openai`) or we fell back to heuristics (`fallback`), which is useful for debugging and analytics.
- `planner_model`, `planner_attempts`, and `planner_error` track the planner’s OpenAI call. Log `planner_error` whenever the model returned empty/invalid JSON so we can tune prompts/models downstream while the fallback keeps the pipeline moving.

CTA validation rules:

- If `cta` is **true**, then `call_to_action` **must** be a non-null, non-empty string.
- If `cta` is **false**, then `call_to_action` **must** be `null` or omitted; consumers must ignore any non-null value in this case.
- Payloads with `cta=true` and `call_to_action=null` (or empty string) should be treated as invalid and either rejected with a 400 or corrected by the planner before persistence.

Consumers should:

- Treat `cta=true` + non-empty `call_to_action` as a strong signal to render a prominent CTA block for that section.
- Safely ignore `call_to_action` whenever `cta=false`.

Future schema types (FAQPage, HowTo) will reuse this envelope but swap the `sections` payload to match their schema.

---

## 6. Developer Usage and Experience (DX)

### 6.1 Example: YouTube → Markdown blog (sync)

```http
POST /v1/content/blog_from_youtube HTTP/1.1
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json

{
  "youtube_url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "mode": "markdown",
  "format": "mdx",
  "tone": "energetic",
  "max_sections": 5,
  "instructions": "Focus on actionable tips for indie hackers."
}
```

- Response: `200 OK`, `Content-Type: text/plain`, MDX body.

### 6.2 Example: Text → structured JSON (async)

```http
POST /v1/content/blog_from_text?async=true HTTP/1.1
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json

{
  "text": "Long transcript...",
  "title": "Keynote on Growth",
  "mode": "structured",
  "format": "json",
  "tone": "professional",
  "instructions": "Highlight growth strategies and avoid deep technical detail."
}
```

- Response: `202 Accepted` with `job_id`.
- Client then polls `GET /v1/jobs/{job_id}` and later `GET /v1/documents/{document_id}/versions/latest?mode=structured&format=json`.

### 6.3 Authentication, API Keys, and Documentation

**Authentication model** (reusing existing mechanisms):

- API consumers obtain a key via the existing protected endpoint:
  - `POST /auth/keys` → returns `APIKeyResponse { api_key, created_at, message }`.
- The key is passed to all `/v1/...` endpoints using a standard bearer header:
  - `Authorization: Bearer YOUR_API_KEY`.
- Internally, the same auth middleware and `get_current_user` dependency used by existing `/api/v1/...` routes can be reused for the new `/v1/content`, `/v1/documents`, and `/v1/jobs` routers.

**How developers discover and use the Content API:**

- Public docs should expose a **curated section** for the Content API that:
  - Explains how to create an API key.
  - Shows end-to-end flows using the examples from §6.1 and §6.2.
  - Documents all `/v1/content`, `/v1/documents`, and `/v1/jobs` endpoints with request/response schemas.
- The existing auto-generated docs (`/docs` / OpenAPI) are currently cluttered with internal and UI-facing routes.
  - This spec should drive a **cleaner, user-facing grouping** in the generated docs (e.g. tagging the new routers as "Content API" and keeping internal endpoints under separate tags).
  - Future work (separate PR) can hide or de-emphasize internal-only endpoints from public documentation while keeping them available for internal use.

---

## 7. Implementation Notes for Future PR

This plan is meant for a separate implementation PR and a follow-up that adds publish/unpublish semantics. High-level steps for that work (not executed now):

1. **Routing & versioning**
   - Add a new router module (e.g. `content_api.py`) with prefix `/v1/content`.
   - Add `/v1/documents` and `/v1/jobs` wrappers reusing existing logic.

2. **Schema changes**
   - Introduce `instructions: Optional[str]` in the appropriate model(s).
   - Optionally define new Pydantic models representing the public request/response shapes for `/v1/content/*`.

3. **Orchestration**
   - Implement `blog_from_youtube` and `blog_from_text` endpoints that:
     - Call existing ingestion and blog generation helpers.
     - Honor `async` flag.
     - Translate `mode` + `format` into the correct underlying calls.

4. **Documentation & examples**
   - Publish this schema in public API docs / OpenAPI with clear examples.

5. **Publish/unpublish (future)**
   - Add `POST /v1/documents/{id}/publish` and `/unpublish` once:
     - Document `status`/`published_at`/`published_version_id` columns are in place.
     - Drive sync semantics for drafts vs published folders are finalized.
   - Update SDKs and docs to surface these endpoints as the recommended way to manage live content.

This file serves as the reference design for that work and future iterations.
