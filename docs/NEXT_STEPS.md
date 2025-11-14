

- P3 — YouTube job (smallest viable)
  - Schema: add `job_type` to `jobs` table and models.
  - API/UI: add `POST /jobs/youtube` (validate URL) and route job to queue with `job_type`.
  - Worker: route by `job_type`; implement `process_youtube_job` to fetch and store title/description.
  - UI: display YouTube job details in job detail page (type/input fields already generic).

- P3 — Integrations page polish
  - ✅ Already implemented: Uses `google_connected` logic, shows Gmail/YouTube with "Coming soon" badges.
  - Consider adding more polish/UX improvements if needed.

- Testing & Observability 
  - Add tests covering CSRF-protected endpoints (retry, cancel, disconnect).
