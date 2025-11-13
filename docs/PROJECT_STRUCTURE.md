# Project Structure

This document describes the organization of the Google Drive Image Optimizer project.

## Directory Structure

```
.
├── api/                    # FastAPI web application
│   ├── __init__.py
│   ├── main.py            # FastAPI app entry point
│   ├── auth.py            # Authentication (GitHub OAuth, JWT, API keys)
│   ├── config.py          # Configuration management
│   ├── database.py        # D1 database utilities
│   ├── models.py          # Pydantic models
│   ├── exceptions.py     # Custom exception classes
│   ├── middleware.py      # Request middleware (auth, rate limiting, etc.)
│   ├── app_logging.py    # Structured logging
│   └── cloudflare_queue.py # Cloudflare Queues integration
│
├── core/                   # Core business logic (shared between CLI and API)
│   ├── __init__.py
│   ├── drive_utils.py      # Google Drive API utilities
│   ├── filename_utils.py   # Filename parsing/sanitization helpers
│   └── image_processor.py  # Image processing and optimization
│
├── workers/                # Background workers
│   ├── __init__.py
│   └── consumer.py        # Queue consumer for processing jobs
│
├── tests/                  # Test suite
│   ├── __init__.py
│   ├── test_api.py        # API endpoint tests
│   ├── test_server.py     # Server startup tests
│   └── test_local.py      # Local integration tests
│
├── migrations/             # Database migrations
│   └── schema.sql         # D1 database schema
│
├── scripts/                # Utility scripts
│   └── fix_pyenv_path.sh  # Python environment setup script
│
├── cli.py                 # CLI entry point (original tool)
├── run_api.py             # Script to run FastAPI server locally
├── requirements.txt       # Python dependencies
├── wrangler.toml          # Cloudflare Workers configuration
├── README.md              # Project documentation
└── docs/                  # Documentation
    ├── DEPLOYMENT.md      # Deployment guide
    └── PROJECT_STRUCTURE.md # This file
```

## Key Components

### API (`api/`)
The FastAPI web application with production-ready features:
- Authentication and authorization
- Database operations
- Request/response models
- Middleware (auth, rate limiting, security headers)
- Error handling
- Logging

### Core (`core/`)
Shared business logic used by both the CLI and API:
- Google Drive integration
- Image processing and optimization
- Reusable utilities

### Workers (`workers/`)
Background job processors:
- Queue consumers for async image optimization
- Long-running tasks

### Tests (`tests/`)
Test suite for the API and core functionality.

### Migrations (`migrations/`)
Database schema and migration files.

## Usage

### Running the CLI
```bash
python cli.py --drive-folder "FOLDER_ID"
```

### Running the API
```bash
python run_api.py
# or
uvicorn api.main:app --reload
```

### Running Tests
```bash
pytest tests/
```

## Import Guidelines

- **API code** should import from `api.*` using relative imports (`.config`, `.models`, etc.)
- **Core code** should import from `core.*` 
- **Workers** should import from both `api.*` and `core.*`
- **CLI** should import from `core.*`
- **Tests** should import from `api.*` and `core.*`

