#!/usr/bin/env python3
"""
Simple script to run the FastAPI server locally.

================================================================================
NOTE: This module is for LOCAL DEVELOPMENT ONLY.
It is NOT used in the Cloudflare Python Worker runtime.

The Cloudflare Worker uses src/workers/main.py as its entrypoint,
which uses Cloudflare's built-in `asgi` module to handle requests
without Uvicorn.

For local development: python run_api.py
For Cloudflare Worker: wrangler dev (or deploy)
================================================================================
"""

import os
import sys
from pathlib import Path
import uvicorn

# Add src/workers to PYTHONPATH for local development
# This allows imports like "from api.main" to work locally
_workers_path = Path(__file__).parent / "src" / "workers"
if _workers_path.exists():
    # Use absolute path to ensure it works from any directory
    workers_path_str = str(_workers_path.resolve())
    
    # Set PYTHONPATH environment variable BEFORE any imports
    # Uvicorn with reload=True spawns subprocesses that need PYTHONPATH
    current_pythonpath = os.environ.get("PYTHONPATH", "")
    if current_pythonpath:
        # Prepend to existing PYTHONPATH if it exists
        if workers_path_str not in current_pythonpath.split(os.pathsep):
            os.environ["PYTHONPATH"] = os.pathsep.join([workers_path_str, current_pythonpath])
    else:
        os.environ["PYTHONPATH"] = workers_path_str
    
    # Also add to sys.path for this process
    if workers_path_str not in sys.path:
        sys.path.insert(0, workers_path_str)

if __name__ == "__main__":
    uvicorn.run(
        "api.main:app",  # Module path matches src/workers added to PYTHONPATH
        host=os.getenv("HOST", "127.0.0.1"),  # Default to localhost for security
        port=8000,
        reload=True,  # Auto-reload on code changes
        log_level="info"
    )

