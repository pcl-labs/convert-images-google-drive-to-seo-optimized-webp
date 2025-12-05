# Coding Rules and Testing Guidelines

These rules capture lessons from Phase 1 to reduce regressions and improve consistency across future work.

## API and Routing
- Use `/api` for JSON API surface; `/` may be HTML. Tests targeting JSON should hit `/api` endpoints.
- OAuth start endpoints (e.g., `/auth/google/start`) redirect (302/303/307) when configured; tests should assert redirects only when config is present, otherwise skip.
- Keep imports at module scope (e.g., `import re`)—avoid inline imports in functions unless required for optional deps.

## Request Validation
- Prefer strong Pydantic types (e.g., `HttpUrl`) and validators for domain-specific checks (e.g., YouTube `youtube.com`/`youtu.be`).
- Add length and value constraints for user-provided text.
- Fail fast with `HTTPException(status_code=400, detail=...)` on invalid inputs.

## Database and Schema
- Maintain referential integrity with foreign keys and `ON DELETE CASCADE` where appropriate.
- For SQLite dev, apply idempotent schema ensures on startup; for D1/production, add explicit migrations to `migrations/schema.sql`.
- Keep JSON payloads in `TEXT` columns (`output`, `metadata`) and parse/serialize carefully.

## Queue and Workers
- Validate queue messages before enqueue (`send_generic`):
  - Job messages must include `job_id`, `user_id`, `job_type`.
  - Reject and log unknown shapes.
- In workers, on invalid message payloads, always:
  - Update job status to `failed` and record an error reason.
  - Avoid leaving jobs in `pending`/`processing` without resolution.

## Models and Responses
- Include `job_type`, `document_id`, and (when applicable) `output` in job responses to enable UI and client logic.
- Centralize mapping constants (e.g., `KIND_MAP`) at module level to avoid duplication.

## Testing
- Use `pytest.mark.asyncio` for async tests; avoid `asyncio.run()` inside tests/fixtures.
- Prefer yield-style fixtures for resources and global state:
  - Set up `Database`, set `set_db_instance(...)` and `set_queue_producer(...)` before tests.
  - Yield control; then close the DB and reset global state after tests.
- Avoid masking failures: tests should not accept 500s as success. Skip when configuration is intentionally absent.
- Mock external systems (queues, OAuth) with minimal validated behaviors.

## Security and Middleware
- Enforce CSRF for state-changing dashboard endpoints and preserve consistent cookie attributes.
- Rate-limiting should default to in-memory in dev; plan for distributed stores in prod.

## Logging
- Sanitize sensitive info in logs; trim excessively long messages.
- Use structured logs with context (job_id, user_id, event) for traceability.

## Style and Hygiene
- Keep imports at file top; avoid mid-function imports.
- Do not duplicate mapping dicts—factor out constants.
- Prefer explicit errors and clear docstrings for public methods.

---

Adopt these rules in PR templates and code reviews to keep the codebase consistent and resilient.
