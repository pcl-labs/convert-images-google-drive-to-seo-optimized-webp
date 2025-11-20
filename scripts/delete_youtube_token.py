#!/usr/bin/env python3
"""Delete YouTube token for a user to force reconnection."""
import asyncio
import sys
from src.workers.api.database import Database, delete_google_tokens

async def main():
    if len(sys.argv) < 2:
        print("Usage: delete_youtube_token.py <user_id>", file=sys.stderr)
        sys.exit(1)

    user_id = sys.argv[1]

    if user_id == "user_id_placeholder":
        print("Error: replace 'user_id_placeholder' with a real user_id to perform deletion.", file=sys.stderr)
        sys.exit(1)

    try:
        db = Database()
        await delete_google_tokens(db, user_id, integration="youtube")
        print(f"✅ Deleted YouTube token for user: {user_id}")
        print("Now reconnect YouTube via /dashboard/integrations/youtube")
    except Exception as e:
        print(f"❌ Failed to delete YouTube token: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

