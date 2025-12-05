#!/usr/bin/env python3
"""Test the YouTube transcript proxy API with a Bearer token."""
import asyncio
import sys
from typing import Optional

import httpx


async def test_youtube_transcript(
    token: str,
    video_id: str = "dQw4w9WgXcQ",
    base_url: str = "https://api-service.getquillio.com",
) -> None:
    """Test the YouTube transcript endpoint with a Bearer token."""
    url = f"{base_url}/api/proxy/youtube-transcript"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"video_id": video_id}

    print(f"Testing: {url}")
    print(f"Video ID: {video_id}")
    print(f"Token: {token[:20]}...")
    print("-" * 60)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            print(f"Status: {response.status_code}")
            print(f"Headers: {dict(response.headers)}")
            print("-" * 60)
            
            try:
                data = response.json()
                import json
                print("Response:")
                print(json.dumps(data, indent=2))
            except Exception:
                print("Response (text):")
                print(response.text)
                
            if response.status_code == 200:
                print("\n✅ Success!")
            else:
                print(f"\n❌ Failed with status {response.status_code}")
                
        except httpx.TimeoutException:
            print("❌ Request timed out")
            sys.exit(1)
        except httpx.RequestError as e:
            print(f"❌ Request error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            sys.exit(1)


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python test_api_with_token.py <BEARER_TOKEN> [video_id] [base_url]")
        print("\nExample:")
        print("  python test_api_with_token.py B9bmEv77UMYBg7Dx7dy4h8EnR0eJMSci")
        print("  python test_api_with_token.py B9bmEv77UMYBg7Dx7dy4h8EnR0eJMSci dQw4w9WgXcQ")
        print("\nTo get your token, run this in your browser console on getquillio.com:")
        print("""
(async () => {
  const response = await fetch('https://getquillio.com/api/auth/get-session', {
    credentials: 'include',
    headers: { 'Accept': 'application/json' }
  });
  const data = await response.json();
  console.log('Token:', data?.session?.token);
  return data?.session?.token;
})();
        """)
        sys.exit(1)
    
    token = sys.argv[1]
    video_id = sys.argv[2] if len(sys.argv) > 2 else "dQw4w9WgXcQ"
    base_url = sys.argv[3] if len(sys.argv) > 3 else "https://api-service.getquillio.com"
    
    asyncio.run(test_youtube_transcript(token, video_id, base_url))


if __name__ == "__main__":
    main()
