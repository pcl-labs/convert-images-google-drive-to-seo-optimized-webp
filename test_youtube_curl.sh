#!/bin/bash
# Test YouTube API with curl before running pytest
# Usage: ./test_youtube_curl.sh YOUR_ACCESS_TOKEN [VIDEO_ID]

ACCESS_TOKEN="${1:-}"
VIDEO_ID="${2:-jNQXAC9IVRw}"  # Default: "Me at the zoo" - first YouTube video

if [ -z "$ACCESS_TOKEN" ]; then
    echo "Usage: $0 ACCESS_TOKEN [VIDEO_ID]"
    echo "Example: $0 ya29.a0AfH6SMB... jNQXAC9IVRw"
    exit 1
fi

echo "Testing YouTube API with video ID: $VIDEO_ID"
echo ""

# Step 1: List captions for the video
echo "1. Listing captions..."
CAPTIONS_RESPONSE=$(curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
    "https://www.googleapis.com/youtube/v3/captions?part=id,snippet&videoId=$VIDEO_ID")

echo "$CAPTIONS_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$CAPTIONS_RESPONSE"
echo ""

# Extract caption ID (first English one)
CAPTION_ID=$(echo "$CAPTIONS_RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    items = data.get('items', [])
    for item in items:
        snippet = item.get('snippet', {})
        lang = snippet.get('language', '').lower()
        if lang.startswith('en'):
            print(item.get('id', ''))
            break
    if not items:
        print('NO_CAPTIONS')
except:
    print('ERROR')
" 2>/dev/null)

if [ -z "$CAPTION_ID" ] || [ "$CAPTION_ID" = "NO_CAPTIONS" ] || [ "$CAPTION_ID" = "ERROR" ]; then
    echo "ERROR: Could not find captions for this video"
    echo "Response was: $CAPTIONS_RESPONSE"
    exit 1
fi

echo "Found caption ID: $CAPTION_ID"
echo ""

# Step 2: Download captions
echo "2. Downloading captions (first 500 chars)..."
CAPTION_TEXT=$(curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
    "https://www.googleapis.com/youtube/v3/captions/$CAPTION_ID?tfmt=srt")

if [ -z "$CAPTION_TEXT" ]; then
    echo "ERROR: Failed to download captions"
    exit 1
fi

echo "Caption text preview (first 500 chars):"
echo "$CAPTION_TEXT" | head -c 500
echo ""
echo ""
echo "Total caption length: ${#CAPTION_TEXT} characters"
echo ""
echo "✅ SUCCESS: Access token works!"
echo "Set your environment variable securely (do not paste tokens into logs):"
echo "   export YOUTUBE_TEST_ACCESS_TOKEN=\"<your token here>\""
echo "Or write the token to a file with restricted permissions and load it:"
echo "   echo '<your token here>' > ~/.yt_token && chmod 600 ~/.yt_token"
echo "   export YOUTUBE_TEST_ACCESS_TOKEN=\"$(cat ~/.yt_token)\""
echo "Then run:"
echo "   pytest -q tests/test_youtube_ingest.py::test_process_ingest_youtube_job_merges_metadata -v"

