# Deployment Guide for Cloudflare Workers

This guide covers deploying the production-ready FastAPI application to Cloudflare Workers.

## Prerequisites

1. Cloudflare account with Workers enabled
2. Wrangler CLI installed: `npm install -g wrangler`
3. GitHub OAuth app created (for authentication)

## Manual Setup Steps (User Action Required)

These steps require manual interaction and cannot be automated:

### 1. Authenticate with Cloudflare

```bash
wrangler login
```

This command opens a browser window where you must log in to your Cloudflare account. The CLI cannot automate this authentication step.

### 2. Create API Token

API tokens cannot be created via the CLI and must be created in the Cloudflare Dashboard:

1. Navigate to: https://dash.cloudflare.com/profile/api-tokens
2. Click "Create Token"
3. Use "Edit Cloudflare Workers" template or create a custom token with these permissions:
   - **Account > Workers Scripts: Edit** (for deploying workers)
   - **Account > Queues: Edit** (required for queue operations)
   - **Account > D1: Edit** (for database operations)
   - **Account > Account Settings: Read** (optional, for account info)
   - **User > User Details: Read** (optional, for user info)
4. Select your account in "Account Resources"
5. Copy the token value immediately (it's only shown once)
6. Save it securely - you'll need it for the `CF_API_TOKEN` environment variable

**Important**: The token value is only displayed once. If you lose it, you'll need to create a new token.

### 3. Get Account ID

After logging in, retrieve your account ID:

```bash
wrangler whoami
```

This outputs your account ID, which you'll need for the `CF_ACCOUNT_ID` environment variable.

Alternatively, you can find your account ID in the Cloudflare dashboard URL when viewing any resource: `https://dash.cloudflare.com/{account_id}/...`

## Automated Setup Steps

These steps can be run via CLI commands:

### 1. Create D1 Database

```bash
# Create the database
wrangler d1 create quill-db
```

The output will include the `database_id`. Update `wrangler.toml` with the database_id from the output:

```toml
[[d1_databases]]
binding = "DB"
database_name = "quill-db"
database_id = "933d76cf-a988-4a71-acc6-d884278c6402"  # Replace with your actual ID
```

**Note**: The database `quill-db` has already been created for this project with ID `933d76cf-a988-4a71-acc6-d884278c6402`.

### 2. Initialize Database Schema

```bash
# Run the schema migration
wrangler d1 execute quill-db --file=migrations/schema.sql
```

### 3. Create Queues

```bash
# Create the main queue
wrangler queues create quill-jobs

# Create the dead letter queue
wrangler queues create quill-dlq
```

**Note**: Queue names should match what's configured in your `wrangler.toml` and environment variables.

**Note**: The queues `quill-jobs` and `quill-dlq` have already been created for this project.

### 4. Configure Environment Variables

For local development, create a `.env` file with:

```bash
# Cloudflare Queue Configuration
USE_INLINE_QUEUE=true                # Set to false for production
CF_ACCOUNT_ID=                        # From wrangler whoami
CF_API_TOKEN=                        # From Cloudflare dashboard (step 2 above)
CF_QUEUE_NAME=quill-jobs             # Primary queue name
CF_QUEUE_DLQ=quill-dlq               # Dead letter queue name
```

For production (Cloudflare Workers), set `USE_INLINE_QUEUE=false` and ensure the API token has the required permissions.

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

### Local Development Environment Variables (`.env` file)

For local development with inline queue mode:

- `USE_INLINE_QUEUE=true` - Use in-memory queue instead of Cloudflare Queue (default for local dev)
- `CF_ACCOUNT_ID` - Cloudflare account ID (from `wrangler whoami`)
- `CF_API_TOKEN` - Cloudflare API token (created in dashboard, step 2 above)
- `CF_QUEUE_NAME=quill-jobs` - Primary queue name
- `CF_QUEUE_DLQ=quill-dlq` - Dead letter queue name

**Note**: When `USE_INLINE_QUEUE=true`, the queue operations run in-memory and don't require Cloudflare Queue API access. Set to `false` for production to use real Cloudflare Queues.

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

