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
  ingest_text, optimize, generate_blog, drive_publish).
- `POST /agent/tools/:tool/invoke` – forward tool calls to the FastAPI backend
  (authorization headers are forwarded automatically).

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
`https://api.example.com`).

## Environment

The Worker can optionally forward messages to the FastAPI backend by pointing
`API_BASE_URL` at its base URL. (Right now it only stores conversations locally;
hook your agent runtime or backend logic up when you are ready.)

## Next Steps

- Wire `/chat/...` routes into the LLM runtime so tool calls trigger the existing
  Python job endpoints.
- Expand the Durable Object to emit pipeline events (ingest jobs, Drive sync,
  etc.) into the SSE stream by listening to queue updates.
