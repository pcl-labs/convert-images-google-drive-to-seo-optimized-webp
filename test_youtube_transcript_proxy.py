#!/usr/bin/env python3
"""
Test script for YouTube transcript proxy endpoint.

Tests the /api/proxy/youtube-transcript endpoint with various video IDs
to confirm structured success/error payloads are returned correctly.

Usage:
    python test_youtube_transcript_proxy.py [API_KEY] [BASE_URL]

Examples:
    python test_youtube_transcript_proxy.py your_api_key
    python test_youtube_transcript_proxy.py your_api_key http://localhost:8787
"""

import sys
import json
import requests
from typing import Dict, Any, Optional

# Test video IDs - mix of valid, invalid, and edge cases
TEST_VIDEO_IDS = [
    "jNQXAC9IVRw",  # "Me at the zoo" - first YouTube video (has captions)
    "dQw4w9WgXcQ",  # Rick Astley - Never Gonna Give You Up (has captions)
    "invalid12345",  # Invalid video ID (should return error)
    "12345678901",   # Valid format but likely doesn't exist
]


def test_transcript_endpoint(
    base_url: str,
    api_key: str,
    video_id: str,
) -> Dict[str, Any]:
    """Test the transcript proxy endpoint with a video ID."""
    url = f"{base_url}/api/proxy/youtube-transcript"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "video_id": video_id,
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        return {
            "status_code": response.status_code,
            "response": response.json(),
            "success": True,
        }
    except requests.exceptions.HTTPError as e:
        # Even HTTP errors should return structured JSON
        try:
            error_response = e.response.json()
            return {
                "status_code": e.response.status_code,
                "response": error_response,
                "success": False,
                "error": str(e),
            }
        except (ValueError, AttributeError):
            return {
                "status_code": getattr(e.response, "status_code", 500),
                "response": {"error": str(e)},
                "success": False,
                "error": str(e),
            }
    except Exception as e:
        return {
            "status_code": None,
            "response": None,
            "success": False,
            "error": str(e),
        }


def print_result(video_id: str, result: Dict[str, Any]) -> None:
    """Print test result in a readable format."""
    print(f"\n{'='*80}")
    print(f"Video ID: {video_id}")
    print(f"{'='*80}")
    print(f"Status Code: {result.get('status_code', 'N/A')}")
    
    if result.get("success") and result.get("response"):
        response = result["response"]
        print(f"\n✅ Success: {response.get('success', False)}")
        
        if response.get("success"):
            transcript = response.get("transcript", {})
            metadata = response.get("metadata", {})
            print(f"\nTranscript:")
            print(f"  - Text length: {len(transcript.get('text', ''))} chars")
            print(f"  - Format: {transcript.get('format', 'N/A')}")
            print(f"  - Language: {transcript.get('language', 'N/A')}")
            print(f"  - Track Kind: {transcript.get('track_kind', 'N/A')}")
            print(f"\nMetadata:")
            print(f"  - Method: {metadata.get('method', 'N/A')}")
            print(f"  - Video ID: {metadata.get('video_id', 'N/A')}")
            print(f"  - Client Version: {metadata.get('client_version', 'N/A')}")
            
            # Show first 200 chars of transcript
            text = transcript.get("text", "")
            if text:
                preview = text[:200] + "..." if len(text) > 200 else text
                print(f"\nTranscript Preview:\n{preview}")
        else:
            error = response.get("error", {})
            print(f"\n❌ Error:")
            print(f"  - Code: {error.get('code', 'N/A')}")
            print(f"  - Message: {error.get('message', 'N/A')}")
            if error.get("details"):
                print(f"  - Details: {error.get('details')}")
    elif result.get("error"):
        print(f"\n❌ Request Failed: {result['error']}")
    else:
        print(f"\n⚠️  Unexpected response format")
        print(json.dumps(result.get("response", {}), indent=2))
    
    print(f"\nFull Response JSON:")
    print(json.dumps(result.get("response", {}), indent=2))


def main():
    """Main test function."""
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nError: API key is required")
        print("\nTo get an API key:")
        print("  1. Start the server: wrangler dev")
        print("  2. Authenticate via GitHub OAuth: http://localhost:8787/auth/github/start")
        print("  3. Create API key: POST http://localhost:8787/auth/keys")
        sys.exit(1)
    
    api_key = sys.argv[1]
    base_url = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8787"
    
    # Check if server is running
    try:
        health_response = requests.get(f"{base_url}/health", timeout=5)
        if health_response.status_code != 200:
            print(f"⚠️  Warning: Server health check returned {health_response.status_code}")
    except requests.exceptions.ConnectionError:
        print(f"❌ Error: Cannot connect to {base_url}")
        print("Make sure wrangler dev is running:")
        print("  wrangler dev")
        sys.exit(1)
    except Exception as e:
        print(f"⚠️  Warning: Health check failed: {e}")
    
    print(f"Testing YouTube Transcript Proxy Endpoint")
    print(f"Base URL: {base_url}")
    print(f"API Key: {api_key[:10]}...{api_key[-4:] if len(api_key) > 14 else ''}")
    
    results = []
    for video_id in TEST_VIDEO_IDS:
        result = test_transcript_endpoint(base_url, api_key, video_id)
        results.append((video_id, result))
        print_result(video_id, result)
    
    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    success_count = sum(1 for _, r in results if r.get("response", {}).get("success"))
    total_count = len(results)
    print(f"Tests run: {total_count}")
    print(f"Successful transcript fetches: {success_count}")
    print(f"Errors/expected failures: {total_count - success_count}")
    
    # Check that all responses have structured format
    structured_count = sum(
        1 for _, r in results
        if r.get("response") and isinstance(r.get("response"), dict)
    )
    print(f"Structured responses: {structured_count}/{total_count}")
    
    if structured_count == total_count:
        print("\n✅ All responses have structured success/error payloads!")
    else:
        print(f"\n⚠️  {total_count - structured_count} responses are not properly structured")


if __name__ == "__main__":
    main()
