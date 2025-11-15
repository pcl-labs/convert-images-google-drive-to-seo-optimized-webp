# Deployment Guide for Cloudflare Workers

This guide covers deploying the production-ready FastAPI application to Cloudflare Workers.

## Prerequisites

1. Cloudflare account with Workers enabled
2. Wrangler CLI installed: `npm install -g wrangler`
3. Cloudflare login: `wrangler login`
4. GitHub OAuth app created (for authentication)

## Setup Steps

### 1. Create D1 Database

```bash
# Create the database
wrangler d1 create quill-db

# Note the database_id from the output and update wrangler.toml
```

Update `wrangler.toml` with the database_id:

```toml
[[d1_databases]]
binding = "DB"
database_name = "quill-db"
database_id = "YOUR_DATABASE_ID_HERE"
```

### 2. Initialize Database Schema

```bash
# Run the schema migration
wrangler d1 execute quill-db --file=migrations/schema.sql
```

### 3. Create Queues

```bash
# Create the main queue
wrangler queues create quill-queue

# Create the dead letter queue
wrangler queues create quill-dlq
```

### 4. Set Secrets

```bash
# GitHub OAuth credentials
wrangler secret put GITHUB_CLIENT_ID
wrangler secret put GITHUB_CLIENT_SECRET

# JWT secret (generate a strong random string)
wrangler secret put JWT_SECRET_KEY

# Encryption key for Fernet (32-byte base64 URL-safe). Generate with:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
wrangler secret put ENCRYPTION_KEY

# Optional: Set redirect URI
wrangler secret put GITHUB_REDIRECT_URI
```

### 5. Configure GitHub OAuth App

1. Go to GitHub Settings > Developer settings > OAuth Apps
2. Create a new OAuth App
3. Set Authorization callback URL to your deployed API URL + `/auth/github/callback`
4. Copy Client ID and Client Secret

### 6. Deploy

```bash
# Deploy the main API worker
wrangler deploy

# Deploy the queue consumer (if separate)
# Note: Queue consumer can be part of the same worker or separate
```

### 7. Verify Deployment

```bash
# Check health endpoint
curl https://your-worker.your-subdomain.workers.dev/health

# Check API docs
open https://your-worker.your-subdomain.workers.dev/docs
```

## Environment Variables

The following environment variables can be set via `wrangler secret put`:

- `GITHUB_CLIENT_ID` - GitHub OAuth Client ID (required)
- `GITHUB_CLIENT_SECRET` - GitHub OAuth Client Secret (required)
- `JWT_SECRET_KEY` - Secret key for JWT tokens (required)
- `ENCRYPTION_KEY` - Base64 URL-safe 32-byte Fernet key for encrypting sensitive data at rest (required)
- `GITHUB_REDIRECT_URI` - OAuth redirect URI (optional, defaults to callback URL)
- `ENVIRONMENT` - Environment name (optional, defaults to "production")
- `DEBUG` - Enable debug mode (optional, defaults to "false")

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

- [ ] D1 database created and schema migrated
- [ ] Queues created and configured
- [ ] All secrets set via `wrangler secret put`
  - [ ] ENCRYPTION_KEY set (Fernet key). Plan key rotation and data re-encryption outside of runtime.
- [ ] GitHub OAuth app configured with correct callback URL
- [ ] Health endpoint responding
- [ ] Authentication flow working
- [ ] Queue processing working
- [ ] Monitoring set up
- [ ] Error logging configured
- [ ] Rate limiting tested
- [ ] CORS configured for frontend (if applicable)

