#!/usr/bin/env python3
import sys
import json
from get_transcript import get_transcript

def test_transcript(video_id):
    """Test the transcript retrieval with a given video ID"""
    print(f"Testing transcript retrieval for video ID: {video_id}")
    result = get_transcript(video_id)
    
    try:
        parsed_result = json.loads(result)
        if parsed_result["success"]:
            print("✅ Success! Transcript retrieved successfully.")
            print(f"Transcript length: {len(parsed_result['transcript'])} characters")
            print("First 200 characters of transcript:")
            print(parsed_result["transcript"][:200] + "...")
        else:
            print("❌ Failed to retrieve transcript.")
            print(f"Error: {parsed_result.get('error', 'Unknown error')}")
    except json.JSONDecodeError:
        print("❌ Failed to parse result as JSON.")
        print(f"Raw result: {result}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 test_transcript.py <video_id>")
        sys.exit(1)
    
    video_id = sys.argv[1]
    test_transcript(video_id) 