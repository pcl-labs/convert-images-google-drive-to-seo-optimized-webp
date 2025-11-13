#!/usr/bin/env python3
"""
Simple script to run the FastAPI server locally.
"""

import os
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "api.main:app",
        host=os.getenv("HOST", "127.0.0.1"),  # Default to localhost for security
        port=8000,
        reload=True,  # Auto-reload on code changes
        log_level="info"
    )

