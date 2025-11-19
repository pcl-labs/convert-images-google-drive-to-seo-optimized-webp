#!/usr/bin/env python3
"""Extract Google OAuth access token from database for testing.
    
WARNING: This utility script prints tokens in plain text for testing purposes only.
Do not use in production or commit tokens to version control.
"""
import asyncio
import sys
from src.workers.api.database import Database, get_google_token
from src.workers.api.utils import redact_token

async def main():
    # Extract user_id from JWT or use provided user_id
    if len(sys.argv) > 1:
        user_id = sys.argv[1]
    else:
        # Default: extract from the JWT you provided
        # JWT payload: {"user_id": "github_5694308", "github_id": "5694308", ...}
        user_id = "github_5694308"
    
    try:
        db = Database()
        # Try YouTube integration first
        token = await get_google_token(db, user_id, "youtube")
        if token:
            access_token = token.get("access_token")
            refresh_token = token.get("refresh_token")
            if access_token:
                print(f"Found YouTube token for user {user_id}:")
                # NOTE: Full tokens printed for testing - be careful with output
                print(f"export YOUTUBE_TEST_ACCESS_TOKEN=\"{access_token}\"")
                if refresh_token:
                    print(f"export YOUTUBE_TEST_REFRESH_TOKEN=\"{refresh_token}\"")
                print(f"\nToken preview (redacted): {redact_token(access_token)}")
                return
        
        # Try other integrations
        print(f"No YouTube token found for user {user_id}")
        print("\nChecking other integrations...")
        
        for integration in ["drive", "gmail"]:
            token = await get_google_token(db, user_id, integration)
            if token:
                access_token = token.get("access_token")
                if access_token:
                    print(f"Found {integration} token (may work for YouTube if scopes match)")
                    # NOTE: Full token printed for testing - be careful with output
                    print(f"export YOUTUBE_TEST_ACCESS_TOKEN=\"{access_token}\"")
                    return
        
        print("No Google tokens found. You may need to:")
        print("1. Link your Google account via the web UI")
        print("2. Or provide a Google OAuth access token directly")
    except Exception as e:
        print(f"❌ Failed to retrieve Google token: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

