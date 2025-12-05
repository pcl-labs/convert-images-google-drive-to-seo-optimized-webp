"""
Legacy authentication utilities - DEPRECATED.

This module contained legacy OAuth login functions (GitHub/Google login flows),
JWT token generation/verification, and API key management that are no longer used.

Authentication is now handled by Better Auth via better_auth.py and deps.py.
OAuth integration linking (Google Drive, YouTube, etc.) is handled by google_oauth.py.

This file is kept for backward compatibility but should not be imported by new code.
All functions have been removed as they are no longer needed.
"""

# This file is intentionally empty - all legacy auth code has been removed.
# If you need authentication, use Better Auth via better_auth.py.
# If you need OAuth integration linking, use google_oauth.py.
