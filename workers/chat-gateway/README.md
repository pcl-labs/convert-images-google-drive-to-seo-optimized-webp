# Chat Gateway Worker

This Worker exposes the Codex-style chat/session surface that sits in front of the
existing FastAPI document APIs. It stores conversations in a Durable Object so we
can offer:

- `POST /chat/sessions` – create a new chat session (Durable Object id).
- `GET /chat/sessions/:id` – load session metadata.
- `GET /chat/sessions/:id/messages` – list messages.
- `POST /chat/sessions/:id/messages` – append a user/assistant message.
- `GET /chat/sessions/:id/events` – Server-Sent Events stream with snapshots +
  new message notifications.
- `GET /agent/tools` – list the available Codex-style tools (ingest_youtube,
  ingest_text, optimize, generate_blog, drive link/status/publish, document
  versions/exports, session events, etc.).
- `POST /agent/tools/:tool/invoke` – forward tool calls to the FastAPI backend
  (authorization headers and `X-Agent-Session-Id` are forwarded automatically,
  plus whitelisted cookies such as `session_id` or `CF_Authorization`).

## Development

```bash
cd workers/chat-gateway
npm install
npm run dev        # wrangler dev --local
npm run deploy     # wrangler deploy
```

`wrangler.toml` already declares the `CHAT_SESSIONS` Durable Object. Use
`wrangler migrations apply` the first time you deploy.

Set `API_BASE_URL` in `wrangler.toml` (or via `wrangler secret put`) so the
`/agent/tools/*` routes know where to forward requests (e.g.
`https://api.example.com`). Set `CHAT_GATEWAY_URL` in your main `.env` so other
services (Nuxt, CLI) know which Worker origin to call.

## Environment

The Worker always forwards tool invocations to the FastAPI backend configured by
`API_BASE_URL`. Each request:

- Injects `X-Agent-Session-Id` (the Durable Object id) so downstream events are
  scoped.
- Filters cookies through a whitelist (`session_id`, `CF_Authorization`, etc.)
  instead of blindly forwarding every browser cookie.
- Streams `/chat/sessions/{id}/events` by polling FastAPI’s
  `/api/v1/sessions/events` endpoint, batching pipeline updates + job snapshots
  into the SSE stream you connect to.

## Operational Notes

- Always create a session first, then send `X-Agent-Session-Id` on every tool
  call (or let the Worker do it on your behalf when using `/agent/tools`).
- If you add new backend cookies, update `FORWARDED_COOKIE_NAMES` in
  `src/index.ts` to allowlist them explicitly.
- The SSE stream uses a simple polling loop (default ~3s) so it works across the
  Cloudflare Worker runtime even when HTTP streaming is buffered. Adjust the
  interval or switch to Durable Object storage if you need lower latency later.
