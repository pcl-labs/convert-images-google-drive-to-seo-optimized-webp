# Deployment Guide for Cloudflare Workers


### 5. Set Secrets

```bash
# GitHub OAuth credentials
wrangler secret put GITHUB_CLIENT_ID
wrangler secret put GITHUB_CLIENT_SECRET

# JWT secret (generate a strong random string)
wrangler secret put JWT_SECRET_KEY

# Encryption key for Fernet (32-byte base64 URL-safe). Generate with:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
wrangler secret put ENCRYPTION_KEY

# Google OAuth client (single client with Drive + YouTube scopes)
wrangler secret put GOOGLE_CLIENT_ID
wrangler secret put GOOGLE_CLIENT_SECRET

# Optional: Set redirect URI
wrangler secret put GITHUB_REDIRECT_URI
```

### 6. Configure GitHub OAuth App

1. Go to GitHub Settings > Developer settings > OAuth Apps
2. Create a new OAuth App
3. Set Authorization callback URL to your deployed API URL + `/auth/github/callback`
4. Copy Client ID and Client Secret

### 7. Deploy

```bash
# Deploy the main API worker
wrangler deploy

# Deploy the queue consumer (if separate)
# Note: Queue consumer can be part of the same worker or separate
```

### 8. Verify Deployment

```bash
# Check health endpoint
curl https://your-worker.your-subdomain.workers.dev/health

# Check API docs
open https://your-worker.your-subdomain.workers.dev/docs
```

## Queue Configuration Modes

- Inline (local dev): `USE_INLINE_QUEUE=true` executes jobs via DB polling. No Cloudflare Queue required. Start the consumer locally with `python workers/consumer.py --inline`.
- Cloudflare Queues (production): `USE_INLINE_QUEUE=false` requires `JOB_QUEUE`/`DLQ` bindings in `wrangler.toml` and secrets `CF_ACCOUNT_ID`, `CF_API_TOKEN`, plus `CF_QUEUE_NAME`/`CF_QUEUE_DLQ`.
- Validation: In production, inline mode is rejected by `api/config.py` to prevent misconfiguration.

## Queue Verification & Troubleshooting

1. Submit a job (optimize or ingest) via API; observe logs:
   - `wrangler tail` should show an enqueue log from `api/cloudflare_queue.py`.
   - Job status should move from `pending` to `processing` shortly after.
2. Check Cloudflare Dashboard > Workers > Queues:
   - `quill-jobs` should receive messages; DLQ (`quill-dlq`) should be empty under normal operation.
3. If jobs remain `pending`:
   - Confirm `USE_INLINE_QUEUE` is set correctly for the environment.
   - Verify `wrangler.toml` queue bindings match actual queue names.
   - Ensure `CF_ACCOUNT_ID`/`CF_API_TOKEN` are set and token has Queues:Edit.
   - Inspect errors emitted by `CloudflareQueueAPI.send` (status/response body).

## Environment Variables

### Cloudflare Workers Secrets (set via `wrangler secret put`)

These are set as secrets in Cloudflare Workers:

- `GITHUB_CLIENT_ID` - GitHub OAuth Client ID (required)
- `GITHUB_CLIENT_SECRET` - GitHub OAuth Client Secret (required)
- `JWT_SECRET_KEY` - Secret key for JWT tokens (required)
- `ENCRYPTION_KEY` - Base64 URL-safe 32-byte Fernet key for encrypting sensitive data at rest (required)
- `GITHUB_REDIRECT_URI` - OAuth redirect URI (optional, defaults to callback URL)
- `ENVIRONMENT` - Environment name (optional, defaults to "production")
- `DEBUG` - Enable debug mode (optional, defaults to "false")
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` - OAuth client used for Drive/YouTube integrations (each integration requests the scopes it needs).

### Local Development Environment Variables (`.env` file)

For local development with inline queue mode:

- `USE_INLINE_QUEUE=true` - Use in-memory queue instead of Cloudflare Queue (default for local dev)
- `CF_ACCOUNT_ID` - Cloudflare account ID (from `wrangler whoami`)
- `CF_API_TOKEN` - Cloudflare API token (created in dashboard, step 2 above)
- `CF_QUEUE_NAME=quill-jobs` - Primary queue name
- `CF_QUEUE_DLQ=quill-dlq` - Dead letter queue name

**Note**: When `USE_INLINE_QUEUE=true`, the queue operations run in-memory and don't require Cloudflare Queue API access. Set to `false` for production to use real Cloudflare Queues.

### Google APIs

- Enable both **Google Drive API** and **YouTube Data API v3** on the same Google Cloud project.
- Configure the OAuth consent screen with the scopes listed in `core/constants.py` (`GOOGLE_INTEGRATION_SCOPES`). Each integration (drive, youtube, gmail) runs its own OAuth flow.
- Users only link their Google account once; missing scopes cause `/ingest/youtube` to return `400` with a helpful message instead of falling back to unofficial transcript scraping.

## Local Development

For local development with Wrangler:

```bash
# Start local development server
wrangler dev

# Run database migrations locally
wrangler d1 execute quill-db --local --file=migrations/schema.sql
```

## Testing

```bash
# Run tests
pytest tests/

# Run with coverage
pytest --cov=. tests/
```

## Monitoring

- View logs: `wrangler tail`
- Monitor queue: Cloudflare Dashboard > Workers > Queues
- Database metrics: Cloudflare Dashboard > D1

## Troubleshooting

### Database Connection Issues

- Verify database_id in wrangler.toml matches created database
- Check database bindings are correct
- Ensure schema is migrated

### Queue Issues

- Verify queue names match in wrangler.toml
- Check queue bindings
- Monitor queue metrics in dashboard

### Authentication Issues

- Verify GitHub OAuth credentials are set correctly
- Check redirect URI matches GitHub app configuration
- Ensure JWT_SECRET_KEY is set

## Production Checklist

### Manual Steps
- [ ] Authenticated with Cloudflare (`wrangler login`)
- [ ] Created API token in Cloudflare dashboard with required permissions:
  - [ ] Workers Scripts: Edit
  - [ ] Queues: Edit
  - [ ] D1: Edit
- [ ] Retrieved account ID (`wrangler whoami`)
- [ ] GitHub OAuth app configured with correct callback URL

### Automated Steps
- [ ] D1 database created and schema migrated
- [ ] Queues created (`quill-jobs` and `quill-dlq`)
- [ ] All secrets set via `wrangler secret put`
  - [ ] ENCRYPTION_KEY set (Fernet key). Plan key rotation and data re-encryption outside of runtime.
- [ ] Environment variables configured (`.env` for local, secrets for production)
- [ ] `wrangler.toml` configured with queue bindings
- [ ] Health endpoint responding
- [ ] Authentication flow working
- [ ] Queue processing working
- [ ] Monitoring set up
- [ ] Error logging configured
- [ ] Rate limiting tested
- [ ] CORS configured for frontend (if applicable)
