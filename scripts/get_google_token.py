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
    
    # Optional: specify which integration to extract (youtube, drive, or all)
    integration_filter = None
    if len(sys.argv) > 2:
        integration_filter = sys.argv[2].lower()
    
    try:
        db = Database()
        integrations_to_check = []
        if integration_filter:
            integrations_to_check = [integration_filter]
        else:
            integrations_to_check = ["youtube", "drive", "gmail"]
        
        found_any = False
        for integration in integrations_to_check:
            token = await get_google_token(db, user_id, integration)
            if token:
                access_token = token.get("access_token")
                refresh_token = token.get("refresh_token")
                if access_token:
                    found_any = True
                    print(f"\n✅ Found {integration} token for user {user_id}:")
                    # NOTE: Full tokens printed for testing - be careful with output
                    if integration == "youtube":
                        print(f"export YOUTUBE_TEST_ACCESS_TOKEN=\"{access_token}\"")
                        if refresh_token:
                            print(f"export YOUTUBE_TEST_REFRESH_TOKEN=\"{refresh_token}\"")
                    elif integration == "drive":
                        print(f"export DRIVE_TEST_ACCESS_TOKEN=\"{access_token}\"")
                        if refresh_token:
                            print(f"export DRIVE_TEST_REFRESH_TOKEN=\"{refresh_token}\"")
                    elif integration == "gmail":
                        print(f"export GMAIL_TEST_ACCESS_TOKEN=\"{access_token}\"")
                        if refresh_token:
                            print(f"export GMAIL_TEST_REFRESH_TOKEN=\"{refresh_token}\"")
                    print(f"Token preview (redacted): {redact_token(access_token)}")
                    if not integration_filter:
                        print()  # Add spacing between multiple tokens
        
        if not found_any:
            print(f"No Google tokens found for user {user_id}.")
            if integration_filter:
                print(f"Integration '{integration_filter}' not found.")
            print("\nYou may need to:")
            print("1. Link your Google account via the web UI")
            print("2. Or provide a Google OAuth access token directly")
            print("\nUsage:")
            print("  python scripts/get_google_token.py [user_id] [integration]")
            print("  Examples:")
            print("    python scripts/get_google_token.py github_5694308")
            print("    python scripts/get_google_token.py github_5694308 youtube")
            print("    python scripts/get_google_token.py github_5694308 drive")
    except Exception as e:
        print(f"❌ Failed to retrieve Google token: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

