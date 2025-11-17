# Quill

Create SEO Ranking blogs from YouTube. Ship fast, flexible, and SEO-optimized blogs with AI assist out of box. Quill brings the best of the LLM ecosystem.

## Features

- **Automatic Download**: Downloads images from any Google Drive folder
- **WebP Conversion**: Converts images to optimized WebP format
- **SEO Optimization**: Creates SEO-friendly filenames with folder-based prefixes
- **Smart Resizing**: Automatically resizes images to optimal dimensions (1200x900 for landscape, 900x1200 for portrait)
- **Compression**: Compresses images to under 300KB while maintaining quality
- **Batch Processing**: Handles multiple images efficiently
- **Duplicate Prevention**: Skips files that already exist in Drive
- **Automatic Cleanup**: Optionally deletes original images after optimization
- **Error Handling**: Comprehensive error logging and retry mechanisms
- **Drive Workspace Sync**: Connecting Google Drive provisions a Quill workspace (root/Drafts/Published) so documents stay mirrored between Drive and the app.

## Installation

1. Clone the repository:
```bash
git clone https://github.com/pcl-labs/convert-images-google-drive-to-seo-optimized-webp.git
cd convert-images-google-drive-to-seo-optimized-webp
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set up Google Drive + Docs + YouTube Data APIs:
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project or select existing one
   - Enable the **Google Drive API**, **Google Docs API**, and **YouTube Data API v3** on the same project
   - Create OAuth 2.0 credentials (web application) and configure the consent screen to include the Drive + Docs scopes Quill uses:
     - `https://www.googleapis.com/auth/drive`
     - `https://www.googleapis.com/auth/documents`
     - (YouTube ingestion continues to use `https://www.googleapis.com/auth/youtube.force-ssl`)
   - Download the credentials and save as `credentials.json` in the project root
   - Quill now ships lightweight REST clients (see `src/workers/core/google_clients.py`) so you do **not** need `google-api-python-client` or `google-auth-httplib2` installed locally—only your OAuth credentials and the scopes above.

## Usage

### CLI Usage

#### Basic Usage

```bash
python cli.py --drive-folder "YOUR_GOOGLE_DRIVE_FOLDER_ID_OR_LINK"
```

#### Advanced Options

```bash
python cli.py \
  --drive-folder "https://drive.google.com/drive/folders/YOUR_FOLDER_ID" \
  --ext "jpg,jpeg,png,bmp,tiff,heic" \
  --overwrite \
  --cleanup
```

### Web API Usage (FastAPI)

The project includes a FastAPI web server for programmatic access.

#### Start the API Server

```bash
# Option 1: Using the run script
python run_api.py

# Option 2: Using uvicorn directly
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`

**Note:** Before starting the API server, ensure you have set the required environment variables (see Environment Variables section below). At minimum, you need:
- `JWT_SECRET_KEY` - Secret key for JWT token generation (required)

For local development with queue processing, also set:
- `USE_INLINE_QUEUE=true` (default) - Enables in-memory queue for local dev

#### API Endpoints

**Public Endpoints:**
- `GET /` - API information
- `GET /health` - Health check
- `GET /docs` - Interactive API documentation (Swagger UI)
- `GET /redoc` - Alternative API documentation

**Authentication Endpoints:**

*Public (no authentication required):*
- `GET /auth/github/start` - Initiate GitHub OAuth flow (redirects to GitHub)
- `GET /auth/github/callback` - GitHub OAuth callback

*Protected (require authentication):*
- `GET /auth/github/status` - GitHub link status
- `GET /auth/google/start?integration=drive|youtube` - Initiate Google OAuth flow for a specific integration (Drive, YouTube, Gmail)
- `GET /auth/google/callback` - Google OAuth callback
- `GET /auth/google/status` - Google link status
- `GET /auth/providers/status` - Unified provider status (GitHub + Google)
- `GET /auth/me` - Get current user information
- `POST /auth/keys` - Generate API key

**Job Endpoints (require authentication):**
- `POST /api/v1/documents/drive` - Register a Drive folder as a document
- `POST /api/v1/optimize` - Start an optimization job for a document
- `POST /ingest/text` - Ingest text content
- `POST /ingest/youtube` - Ingest YouTube video transcript (requires Google account linked with Drive + YouTube scopes; failures bubble up as descriptive 4xx errors—no fallback scraping)
- `GET /api/v1/jobs/{job_id}` - Get job status
- `GET /api/v1/jobs` - List recent jobs
- `DELETE /api/v1/jobs/{job_id}` - Cancel a job

**Usage Endpoints (require authentication):**
- `GET /api/v1/usage/summary?window=7` - Get usage summary (events, duration, bytes downloaded)
- `GET /api/v1/usage/events?limit=50&offset=0` - List usage events

**Admin Endpoints (require authentication):**
- `GET /api/v1/stats` - Get API statistics

#### Running the Queue Consumer (Local Development)

For local development with `USE_INLINE_QUEUE=true`, you need to run the queue consumer in a separate terminal to process jobs:

```bash
# Start the queue consumer in inline mode
python workers/consumer.py --inline

# Optional: Adjust poll interval (default: 1.0 seconds)
python workers/consumer.py --inline --poll-interval 0.5
```

The consumer will poll the in-memory queue and process jobs as they arrive. Make sure your `.env` has `USE_INLINE_QUEUE=true` (the default).

**Note:** In production (Cloudflare Workers), the consumer runs automatically via queue bindings configured in `wrangler.toml`.

#### Example: Start an Optimization Job

```bash
# Create a Drive-backed document
curl -X POST "http://localhost:8000/api/v1/documents/drive" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -d '{"drive_source":"https://drive.google.com/drive/folders/YOUR_FOLDER"}'

# Use the returned document_id to kick off optimization
curl -X POST "http://localhost:8000/api/v1/optimize" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -d '{
    "document_id": "DOCUMENT_ID_FROM_PREVIOUS_STEP",
    "extensions": ["jpg", "jpeg", "png"],
    "cleanup_originals": false
  }'
```

Response:
```json
{
  "job_id": "uuid-here",
  "status": "pending",
  "progress": {
    "stage": "initializing",
    "downloaded": 0,
    "optimized": 0,
    "uploaded": 0
  },
  "created_at": "2025-01-XX..."
}
```

The job will be queued and processed by the worker consumer running in the other terminal.

#### Example: Check Job Status

```bash
curl -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  "http://localhost:8000/api/v1/jobs/{job_id}"
```

**Note:** Most endpoints require authentication. You can authenticate via:
1. GitHub OAuth: Visit `/auth/github/start` to start the OAuth flow
2. API Key: Use `POST /auth/keys` to generate an API key (requires GitHub auth first)
3. Google OAuth (link integrations):
   - Visit `/auth/google/start?integration=drive` to enable Drive/Doc workflows.
   - Visit `/auth/google/start?integration=youtube` to enable YouTube ingestion.

#### Interactive API Documentation

Visit `http://localhost:8000/docs` in your browser for interactive API testing.

### YouTube ingestion requirements

- Use the **same** Google OAuth client (configured above) with both the Drive API and YouTube Data API enabled. Each integration requests only the scopes it needs, so no additional client IDs are required.
- Link your Google account after logging in:
  - `/auth/google/start?integration=drive` enables Drive-backed documents.
  - `/auth/google/start?integration=youtube` enables YouTube ingestion.
- During ingestion the API fetches metadata (title, description, duration, thumbnails, etc.) from the YouTube Data API and stores it with the document. If Google revokes access or the API denies the request, the job will fail up front with a descriptive error instead of silently falling back to unofficial scraping.
- Transcripts are still retrieved from captions, but the authoritative duration + channel metadata always comes from the official API.

### Drive workspace sync

- When you connect Google Drive, Quill now creates a dedicated workspace inside your Drive account: `Quill/` with nested `Drafts/` and `Published/` folders.
- Every Drive-backed document now gets its own subfolder inside that workspace (e.g., `Quill/<slug>/`) with nested `Drafts/`, `Media/`, and `Published/` folders. Quill automatically creates a Google Doc inside `Drafts` to serve as the canonical draft, and drops generated assets into `Media`.
- When you export or mark a document ready, the final artifact lands inside that document’s `Published/` folder while Quill continues to track the same Drive file ID.
- The integration detail page (`/dashboard/integrations/drive`) shows real-time workspace data (folder links, latest synced file, document counts) so you always know what’s connected.
- This automation requires the Drive + Docs scopes listed above; if you add them to your OAuth client you’ll be prompted to re-consent the next time you reconnect Drive.

### Command Line Arguments

- `--drive-folder`: Google Drive folder ID or share link (required)
- `--ext`: Comma-separated list of image extensions to process (default: jpg,jpeg,png,bmp,tiff,heic)
- `--overwrite`: Overwrite existing optimized files
- `--skip-existing`: Skip files that are already optimized
- `--cleanup`: Automatically delete original images after optimization
- `--max-retries`: Number of retry attempts for failed operations (default: 3)
- `--versioned`: Save versioned filenames if conflicts occur
- `--dry-run`: Preview actions without making changes
- `--reauth`: Force new Google account authentication

## How It Works

1. **Authentication**: Uses OAuth 2.0 to authenticate with Google Drive API
2. **Download**: Downloads all images from the specified Drive folder to a temporary directory
3. **Processing**: For each image:
   - Resizes to optimal dimensions
   - Converts to WebP format
   - Compresses to under 300KB
   - Creates SEO-friendly filename with folder prefix
4. **Upload**: Uploads optimized images back to the same Drive folder
5. **Cleanup**: Optionally deletes original images and removes temporary files

## File Structure

```
├── cli.py                 # CLI entry point
├── api/                   # FastAPI web application
│   └── main.py            # API entry point
├── core/                  # Core business logic
│   ├── drive_utils.py     # Google Drive API utilities
│   ├── filename_utils.py  # Filename parsing/sanitization helpers
│   └── image_processor.py # Image processing
├── run_api.py             # Script to run the API server
├── requirements.txt       # Python dependencies
├── docs/                  # Documentation
└── README.md             # This file
```

## Output

- Optimized images are saved as WebP files with SEO-friendly names
- Original filenames are preserved in the alt text mapping
- Progress and error logs are displayed in the console
- Failed operations are logged to `failures.log`

## Security

- OAuth credentials (`credentials.json`) and tokens (`token.json`) are excluded from version control
- All sensitive files are listed in `.gitignore`
- No API keys or secrets are stored in the repository

## Testing

The project includes a comprehensive test suite using pytest.

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run specific test files
pytest tests/test_api.py -v
pytest tests/test_server.py -v
pytest tests/test_local.py -v  # Requires server to be running
```

### Test Structure

- `tests/test_api.py` - Unit tests for API endpoints (uses TestClient, no server required)
- `tests/test_server.py` - Tests for app initialization and structure
- `tests/test_local.py` - Integration tests against a running server (requires `python run_api.py` to be running)

**Note:** For local integration tests, start the server first:
```bash
export JWT_SECRET_KEY="test-jwt-secret-key-for-testing-only"
python run_api.py
```

Then in another terminal:
```bash
pytest tests/test_local.py -v
```

## Requirements

- Python 3.12+
- **FFmpeg** (required for YouTube audio extraction) - Install via `brew install ffmpeg` (macOS) or your system package manager
- Google Drive API access
- Internet connection for Drive API calls

### Environment variables

Set in `.env` or your shell as needed:

**Required:**
- `JWT_SECRET_KEY` (required) - Secret key for JWT token generation and encryption key derivation
- `ENCRYPTION_KEY` (required in production) - Base64 URL-safe 32-byte key used by the ChaCha20-Poly1305 cipher for encrypting sensitive data

**OAuth (optional):**
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_REDIRECT_URI`
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`

**Application Settings:**
- `ENVIRONMENT` (optional, default: "development") - Environment name (development/production)
- `RATE_LIMIT_PER_MINUTE` (optional, default: 60) - Per-minute rate limit
- `RATE_LIMIT_PER_HOUR` (optional, default: 1000) - Per-hour rate limit

**Cloudflare Queue Configuration (for local development):**
- `USE_INLINE_QUEUE` (default: true) - Use in-memory queue for local dev. Set to `false` for production to use Cloudflare Queues
- `CLOUDFLARE_ACCOUNT_ID` - Cloudflare account ID (get with: `wrangler whoami`)
- `CLOUDFLARE_API_TOKEN` - Cloudflare API token with Queues:Edit permission (create in dashboard)
- `CF_QUEUE_NAME` (default: "quill-jobs") - Cloudflare queue name
- `CF_QUEUE_DLQ` (default: "quill-dlq") - Cloudflare dead letter queue name

**Note:** For local development, set `USE_INLINE_QUEUE=true` (default). The queue will run in-memory and you can process jobs locally using `python workers/consumer.py --inline`.

For production, set `USE_INLINE_QUEUE=false` and provide `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, and `CF_QUEUE_NAME`. See `docs/DEPLOYMENT.md` for detailed setup instructions.

**Transcripts (optional):**
- `TRANSCRIPT_LANGS` (default: "en,en-US,en-GB") - Comma-separated language codes for YouTube transcript fetching

## Dependencies

- `urllib` (standard library, wrapped in [`simple_http.py`](simple_http.py)): Lightweight HTTP client used for Google REST calls without third-party deps
- `Pillow`: Image processing (convert HEIC/HEIF assets to JPEG/PNG before invoking the CLI/Worker)
- `tqdm`: Progress bars
- `fastapi`: Web API framework
- `uvicorn`: ASGI server for FastAPI
- `python-multipart`: Form data support
- `pydantic`: Data validation for request/response models
- `pyjwt`: JWT handling (HMAC-only)
- `pytest`: Testing framework

## Security Features

### Rate Limiting

The application includes in-memory rate limiting middleware that tracks requests per user (or IP address if unauthenticated). Default limits are:
- **Per minute**: 60 requests (configurable via `RATE_LIMIT_PER_MINUTE`)
- **Per hour**: 1000 requests (configurable via `RATE_LIMIT_PER_HOUR`)

**Important Notes:**
- The current implementation uses in-memory storage, which means:
  - Rate limits are **per-instance** - each server instance maintains its own counter
  - In a multi-instance deployment (e.g., multiple Cloudflare Workers), rate limits won't be shared across instances
  - For production deployments with multiple instances, consider migrating to:
    - **Cloudflare KV** for shared rate limit state
    - **Redis** for distributed rate limiting
    - **Cloudflare Rate Limiting** (native CF feature)

### Token Encryption

Google OAuth tokens (`access_token` and `refresh_token`) are encrypted at rest using [ChaCha20-Poly1305](https://cryptography.io/en/latest/hazmat/primitives/aead/#chacha20-poly1305) via the `cryptography` library. Each ciphertext is a URL-safe base64 string containing:

- Version byte
- 96-bit nonce (randomly generated per encryption)
- ChaCha20-Poly1305 ciphertext + authentication tag

The cipher key comes from the `ENCRYPTION_KEY` environment variable (a base64-encoded 32 byte key suitable for ChaCha20-Poly1305).

**Key Management:**
- Generate the key with `python -c "import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`
- **Key Rotation**: deploy a new `ENCRYPTION_KEY`, then cycle user tokens (re-link Google integrations) or run a migration script that decrypts with the old key and re-encrypts with the new key. Once data is re-encrypted you can discard the old key.
- **Backup**: Keep secure backups of both `JWT_SECRET_KEY` and `ENCRYPTION_KEY`; losing either means OAuth tokens become unrecoverable.

## License

This project is open source and available under the MIT License.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

### Coding Rules and DRY Principles

Please review our coding standards before contributing:

- See docs/coding-rules.md for validation, schema integrity, queue message rules, worker failure handling, testing practices, security/logging, and DRY guidance (e.g., centralizing constants like KIND_MAP and shared helpers like parse_youtube_video_id).

## Support

For issues and questions, please open an issue on GitHub. 
