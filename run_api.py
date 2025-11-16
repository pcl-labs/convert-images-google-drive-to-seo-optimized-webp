#!/usr/bin/env python3
"""
Simple script to run the FastAPI server locally.
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
    # Verify PYTHONPATH is set (should already be set above)
    if _workers_path.exists():
        workers_path_str = str(_workers_path.resolve())
        current_pythonpath = os.environ.get("PYTHONPATH", "")
        path_list = current_pythonpath.split(os.pathsep) if current_pythonpath else []
        if workers_path_str not in path_list:
            if current_pythonpath:
                os.environ["PYTHONPATH"] = os.pathsep.join([workers_path_str, current_pythonpath])
            else:
                os.environ["PYTHONPATH"] = workers_path_str
    
    uvicorn.run(
        "api.main:app",  # String import required for reload
        host=os.getenv("HOST", "127.0.0.1"),  # Default to localhost for security
        port=8000,
        reload=True,  # Auto-reload on code changes
        log_level="info"
    )

