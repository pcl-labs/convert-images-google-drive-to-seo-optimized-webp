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

3. Set up Google Drive API:
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project or select existing one
   - Enable the Google Drive API
   - Create credentials (OAuth 2.0 Client ID)
   - Download the credentials and save as `credentials.json` in the project root

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

**Note:** Before starting the API server, ensure you have set the required environment variable:
- `JWT_SECRET_KEY` - Secret key for JWT token generation (required). You can set this in a `.env` file or export it in your shell.

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
- `GET /auth/google/start` - Initiate Google OAuth flow (link Google Drive)
- `GET /auth/google/callback` - Google OAuth callback
- `GET /auth/google/status` - Google link status
- `GET /auth/providers/status` - Unified provider status (GitHub + Google)
- `GET /auth/me` - Get current user information
- `POST /auth/keys` - Generate API key

**Job Endpoints (require authentication):**
- `POST /api/v1/documents/drive` - Register a Drive folder as a document
- `POST /api/v1/optimize` - Start an optimization job for a document
- `POST /ingest/text` - Ingest text content
- `POST /ingest/youtube` - Ingest YouTube video transcript
- `GET /api/v1/jobs/{job_id}` - Get job status
- `GET /api/v1/jobs` - List recent jobs
- `DELETE /api/v1/jobs/{job_id}` - Cancel a job

**Usage Endpoints (require authentication):**
- `GET /api/v1/usage/summary?window=7` - Get usage summary (events, duration, bytes downloaded)
- `GET /api/v1/usage/events?limit=50&offset=0` - List usage events

**Admin Endpoints (require authentication):**
- `GET /api/v1/stats` - Get API statistics

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

#### Example: Check Job Status

```bash
curl -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  "http://localhost:8000/api/v1/jobs/{job_id}"
```

**Note:** Most endpoints require authentication. You can authenticate via:
1. GitHub OAuth: Visit `/auth/github/start` to start the OAuth flow
2. API Key: Use `POST /auth/keys` to generate an API key (requires GitHub auth first)
3. Google OAuth (link account for Drive access): Visit `/auth/google/start` after authenticating with GitHub

#### Interactive API Documentation

Visit `http://localhost:8000/docs` in your browser for interactive API testing.

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

- `JWT_SECRET_KEY` (required) - Secret key for JWT token generation and encryption key derivation
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_REDIRECT_URI`
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`
- `ENVIRONMENT` (optional, default: "development") - Environment name (development/production)
- `RATE_LIMIT_PER_MINUTE` (optional, default: 60) - Per-minute rate limit
- `RATE_LIMIT_PER_HOUR` (optional, default: 1000) - Per-hour rate limit

**Phase 2: Transcripts/ASR** (optional):
- `ENABLE_YTDLP_AUDIO` (default: true) - Enable yt-dlp audio extraction
- `ASR_ENGINE` (default: "faster_whisper") - ASR engine: faster_whisper|whisper|provider
- `WHISPER_MODEL_SIZE` (default: "small.en") - Whisper model size
- `ASR_DEVICE` (default: "cpu") - Device: cpu|cuda|auto
- `ASR_MAX_DURATION_MIN` (default: 60) - Maximum duration in minutes
- `TRANSCRIPT_LANGS` (default: "en,en-US,en-GB") - Comma-separated language codes

## Dependencies

- `google-api-python-client`: Google Drive API client
- `google-auth-httplib2`: Google authentication
- `google-auth-oauthlib`: OAuth 2.0 authentication
- `Pillow`: Image processing
- `pillow-heif`: HEIC image support
- `tqdm`: Progress bars
- `fastapi`: Web API framework
- `uvicorn`: ASGI server for FastAPI
- `python-multipart`: Form data support
- `pydantic` & `pydantic-settings`: Data validation and settings management (V2 compatible)
- `cryptography`: Encryption support for sensitive data at rest
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

Google OAuth tokens (`access_token` and `refresh_token`) are encrypted at rest using Fernet symmetric encryption. The encryption key is derived from `JWT_SECRET_KEY` using SHA256.

**Key Management:**
- The encryption key is derived from `JWT_SECRET_KEY` - ensure this is a strong, randomly generated secret
- **Key Rotation**: If you need to rotate `JWT_SECRET_KEY`:
  1. Generate a new `JWT_SECRET_KEY`
  2. Re-authenticate all users with Google (they'll need to reconnect their Google accounts)
  3. Old encrypted tokens cannot be decrypted with the new key
  4. Alternatively, implement a migration script to decrypt with old key and re-encrypt with new key before rotating
- **Backup**: Keep secure backups of `JWT_SECRET_KEY` - losing it means all encrypted tokens become unrecoverable

## License

This project is open source and available under the MIT License.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

### Coding Rules and DRY Principles

Please review our coding standards before contributing:

- See docs/coding-rules.md for validation, schema integrity, queue message rules, worker failure handling, testing practices, security/logging, and DRY guidance (e.g., centralizing constants like KIND_MAP and shared helpers like parse_youtube_video_id).

## Support

For issues and questions, please open an issue on GitHub. 
