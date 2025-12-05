# YouTube Proxy API

A lean API for proxying YouTube transcript requests and interacting with YouTube, built with FastAPI and designed to run on Cloudflare Workers.

## Features

- **YouTube Transcript Proxy**: Fetch YouTube video transcripts via proxy service
- **Google OAuth**: Authentication for future YouTube API features
- **Image Processing**: Image optimization utilities (retained for future use)
- **OpenAI Integration**: OpenAI client for AI operations
- **Job Management**: Track and manage background jobs

## Architecture

This API is designed to run on Cloudflare Workers using:
- **FastAPI** for the API framework
- **D1** for SQLite database storage
- **Cloudflare Queues** for job processing (minimal usage)
- **Python Workers** runtime

## Setup

### Prerequisites

- Python 3.12+
- Cloudflare account with Workers and D1 enabled
- Google Cloud project with OAuth credentials (for Google OAuth)
- Wrangler CLI installed (`npm install -g wrangler`)

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd youtube-proxy-api
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment variables:
```bash
cp .env.example .env
# Edit .env with your configuration
```

4. Set up Cloudflare D1 database:
```bash
wrangler d1 create <database-name>
# Update wrangler.toml with the database binding
```

5. Run database migrations:
```bash
wrangler d1 execute <database-name> --file=migrations/schema.sql
```

### Environment Variables

Required environment variables (set in `.env` or via `wrangler secret put`):

- `JWT_SECRET_KEY` - Secret key for JWT token generation (required)
- `GOOGLE_CLIENT_ID` - Google OAuth client ID (optional, for Google OAuth)
- `GOOGLE_CLIENT_SECRET` - Google OAuth client secret (optional, for Google OAuth)

Optional:
- `OPENAI_API_KEY` - OpenAI API key (optional, for Cloudflare AI Gateway)
- `OPENAI_API_BASE` - OpenAI API base URL (optional, for Cloudflare AI Gateway)
- `YOUTUBE_PROXY_API_URL` - YouTube proxy service URL (e.g., https://tubularblogs.com)
- `YOUTUBE_PROXY_API_KEY` - YouTube proxy service API key
- `USE_INLINE_QUEUE` - Use in-memory queue for local development (default: true)
- `ENVIRONMENT` - Environment name (development, production)

## Development

### Local Development

1. Start the API server:
```bash
python run_api.py
```

The API will be available at `http://localhost:8000`

2. Access the interactive API documentation:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

**Note:** For local development, set `LOCAL_SQLITE_PATH` environment variable to use a local SQLite database:
```bash
export LOCAL_SQLITE_PATH=$(pwd)/data/dev.db
python run_api.py
```

### Cloudflare Workers Deployment

1. Deploy to Cloudflare Workers:
```bash
wrangler deploy
```

2. Set secrets:
```bash
wrangler secret put JWT_SECRET_KEY
wrangler secret put GOOGLE_CLIENT_ID
wrangler secret put GOOGLE_CLIENT_SECRET
wrangler secret put OPENAI_API_KEY          # Optional, for Cloudflare AI Gateway
wrangler secret put YOUTUBE_PROXY_API_URL   # Optional, YouTube proxy service URL
wrangler secret put YOUTUBE_PROXY_API_KEY  # Optional, YouTube proxy service API key
```

## API Endpoints

**Public Endpoints:**
- `GET /api` - API information
- `GET /health` - Health check
- `GET /docs` - Interactive API documentation (Swagger UI)
- `GET /redoc` - Alternative API documentation

**Authentication Endpoints:**

*Public (no authentication required):*
- `GET /auth/google/start?integration=drive|youtube` - Initiate Google OAuth flow
- `GET /auth/google/callback` - Google OAuth callback

*Protected (require authentication):*
- `GET /auth/google/status` - Google link status
- `GET /auth/providers/status` - Provider status (Google only)
- `GET /auth/me` - Get current user information
- `POST /auth/keys` - Generate API key

**Job Endpoints (require authentication):**
- `GET /api/v1/jobs/{job_id}` - Get job status
- `GET /api/v1/jobs` - List recent jobs
- `DELETE /api/v1/jobs/{job_id}` - Cancel a job

**YouTube Proxy Endpoints:**
- `POST /api/proxy/youtube-transcript` - Proxy YouTube transcript requests (requires API key authentication)

**Debug Endpoints (require authentication):**
- `GET /debug/google` - Debug Google integrations
- `GET /api/v1/debug/env` - Debug environment configuration

## Example: Use YouTube Transcript Proxy

```bash
# Fetch YouTube transcript via proxy
curl -X POST "http://localhost:8000/api/proxy/youtube-transcript" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"video_id": "dQw4w9WgXcQ"}'
```

Response:
```json
{
  "success": true,
  "transcript": {
    "text": "Never gonna give you up...",
    "format": "json3",
    "language": "en"
  },
  "metadata": {
    "client_version": "1.0",
    "method": "innertube",
    "video_id": "dQw4w9WgXcQ"
  }
}
```

## Testing

Run tests with:
```bash
pytest tests/ -v
```

For local integration tests, start the server first:
```bash
export JWT_SECRET_KEY="test-jwt-secret-key-for-testing-only"
export LOCAL_SQLITE_PATH=$(pwd)/data/dev.db
python run_api.py
```

Then in another terminal:
```bash
pytest tests/integration/test_local.py -v
```

## License

MIT
