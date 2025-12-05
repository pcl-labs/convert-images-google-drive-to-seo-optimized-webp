# YouTube Transcript Proxy API Specification

## Overview

This document specifies the API endpoint needed for fetching YouTube video transcripts through a proxy service. The service will be deployed on `tubularblogs.com` and will handle all YouTube Innertube API interactions with built-in proxy rotation.

## Why We Need This

### Problem
- **Cloudflare Workers Limitations**: Cloudflare Workers don't support traditional HTTP proxy agents like Node.js does
- **IP Blocking**: YouTube may block requests from Cloudflare's IP ranges
- **Production Failures**: Innertube method works locally but fails in production due to IP restrictions
- **Complex Logic**: YouTube scraping logic (config extraction, Innertube API calls, transcript parsing) is complex and benefits from being centralized

### Solution
A separate Python service that:
- Handles all YouTube Innertube API interactions
- Manages proxy rotation and validation internally
- Returns clean transcript text to the calling service
- Centralizes YouTube scraping logic for easier maintenance

## Architecture

```
Cloudflare Worker (quill-nuxsaas)
  ↓ HTTP POST request
Python Service (tubularblogs.com)
  ↓ Uses proxy rotation
YouTube Innertube API
  ↓ Response
Python Service
  ↓ Parses & returns
Cloudflare Worker
  ↓ Uses transcript
Content Generation Pipeline
```

## API Endpoint Specification

### Endpoint

```
POST https://tubularblogs.com/api/proxy/youtube-transcript
```

### Authentication

**Header:**
```
Authorization: Bearer YOUR_API_KEY
```

Or in request body (if preferred):
```json
{
  "videoId": "...",
  "apiKey": "your-secret-key"
}
```

### Request

**Content-Type:** `application/json`

**Body:**
```json
{
  "videoId": "dQw4w9WgXcQ"
}
```

**Parameters:**
- `videoId` (string, required): YouTube video ID (e.g., from URL `youtube.com/watch?v=dQw4w9WgXcQ`)

### Response (Success)

**Status Code:** `200 OK`

**Body:**
```json
{
  "success": true,
  "transcript": {
    "text": "Full transcript text here with all the words from the video...",
    "format": "json3",
    "language": "en",
    "trackKind": "asr"
  },
  "metadata": {
    "clientVersion": "2.20231205.00.00",
    "method": "innertube",
    "videoId": "dQw4w9WgXcQ"
  }
}
```

**Fields:**
- `success` (boolean): Always `true` for successful responses
- `transcript.text` (string): The full transcript text, parsed and cleaned
- `transcript.format` (string): Either `"json3"` or `"vtt"` depending on what format was available
- `transcript.language` (string, optional): Language code (e.g., `"en"`, `"es"`)
- `transcript.trackKind` (string, optional): `"asr"` for auto-generated, or `null` for manual captions
- `metadata.clientVersion` (string): YouTube client version used
- `metadata.method` (string): Always `"innertube"` for this endpoint
- `metadata.videoId` (string): Echo of the requested video ID

### Response (Error)

**Status Code:** `200 OK` (or appropriate HTTP error code)

**Body:**
```json
{
  "success": false,
  "error": {
    "code": "no_captions",
    "message": "This video doesn't have captions available",
    "details": "Optional detailed error information for debugging"
  }
}
```

**Error Codes:**
- `no_captions`: Video has no captions/transcripts available
- `blocked`: YouTube is blocking requests (all proxies failed)
- `private`: Video is private or unavailable
- `rate_limited`: Too many requests, rate limited
- `network_error`: Network/proxy connection failed
- `invalid_video`: Video ID is invalid or video doesn't exist
- `unknown`: Unexpected error

## Implementation Requirements

### 1. Proxy Management

The service should implement a proxy manager similar to your existing Python proxy system:

- **Proxy Storage**: Store proxies in JSON file or database
- **Proxy Validation**: Test proxies against `https://www.youtube.com` before use
- **Proxy Rotation**: Rotate through working proxies on each request
- **Automatic Cleanup**: Remove dead proxies periodically (e.g., hourly)
- **Retry Logic**: If one proxy fails, try next proxy (up to N attempts)

### 2. YouTube Innertube Flow

The service needs to replicate the logic currently in `youtubeIngest.ts`:

1. **Fetch Watch Page** (through proxy):
   - GET `https://www.youtube.com/watch?v={videoId}&hl=en`
   - Extract `INNERTUBE_API_KEY` from page HTML
   - Extract `INNERTUBE_CONTEXT_CLIENT_VERSION` from page HTML

2. **Call Innertube API** (through proxy):
   - POST `https://www.youtube.com/youtubei/v1/player?key={apiKey}`
   - Body:
     ```json
     {
       "context": {
         "client": {
           "hl": "en",
           "gl": "US",
           "clientName": "WEB",
           "clientVersion": "{clientVersion}"
         }
       },
       "videoId": "{videoId}"
     }
     ```

3. **Extract Caption Tracks**:
   - Parse `captions.playerCaptionsTracklistRenderer.captionTracks`
   - Select preferred track:
     - Prefer English non-ASR (manual captions)
     - Fallback to any non-ASR
     - Fallback to first available

4. **Download Transcript** (through proxy):
   - Try JSON3 format first: `{track.baseUrl}?fmt=json3`
   - Parse JSON3 events → extract text segments
   - If JSON3 fails, try VTT: `{track.baseUrl}?fmt=vtt`
   - Strip VTT formatting → plain text

5. **Return Clean Text**:
   - Join all segments with spaces
   - Normalize whitespace
   - Return as single string

### 3. Error Handling

- **Proxy Failures**: Try next proxy, up to N attempts (e.g., 3-5)
- **No Captions**: Return error code `no_captions` immediately
- **Network Errors**: Retry with different proxy
- **Rate Limiting**: Return error code `rate_limited`
- **Invalid Video**: Return error code `invalid_video`

### 4. Performance Considerations

- **Timeout**: Set reasonable timeouts (e.g., 30 seconds per request)
- **Caching**: Consider caching client config (API key, version) for a short period
- **Concurrent Requests**: Handle multiple requests efficiently
- **Proxy Pool Size**: Maintain enough working proxies (e.g., 10-20)

## Example Implementation Flow

```python
def fetch_youtube_transcript(video_id: str):
    # 1. Get working proxy
    proxy = proxy_manager.get_working_proxy()
    
    # 2. Fetch watch page through proxy
    watch_page = fetch_with_proxy(
        f"https://www.youtube.com/watch?v={video_id}",
        proxy
    )
    
    # 3. Extract config
    api_key = extract_innertube_api_key(watch_page)
    client_version = extract_client_version(watch_page)
    
    # 4. Call Innertube API through proxy
    player_response = call_innertube_api(
        video_id, api_key, client_version, proxy
    )
    
    # 5. Get caption tracks
    tracks = extract_caption_tracks(player_response)
    if not tracks:
        return {"success": False, "error": {"code": "no_captions", ...}}
    
    # 6. Select preferred track
    track = select_preferred_track(tracks)
    
    # 7. Download transcript through proxy
    transcript_text = download_transcript(track, proxy)
    
    # 8. Return result
    return {
        "success": True,
        "transcript": {
            "text": transcript_text,
            "format": "json3",
            "language": track.get("languageCode"),
            "trackKind": track.get("kind")
        },
        "metadata": {
            "clientVersion": client_version,
            "method": "innertube",
            "videoId": video_id
        }
    }
```

## Integration with Cloudflare Worker

The Cloudflare Worker will call this endpoint like:

```typescript
async function fetchTranscriptViaInnertube(videoId: string) {
  const response = await $fetch('https://tubularblogs.com/api/proxy/youtube-transcript', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${process.env.YOUTUBE_PROXY_API_KEY}`,
      'Content-Type': 'application/json'
    },
    body: { videoId }
  })
  
  if (!response.success) {
    throw new Error(response.error?.message || 'Failed to fetch transcript')
  }
  
  return {
    text: response.transcript.text,
    track: {
      languageCode: response.transcript.language,
      kind: response.transcript.trackKind
    },
    format: response.transcript.format,
    clientVersion: response.metadata.clientVersion
  }
}
```

## Security Considerations

1. **API Key Authentication**: Require valid API key for all requests
2. **Rate Limiting**: Implement rate limiting per API key
3. **Input Validation**: Validate `videoId` format (alphanumeric, 11 characters)
4. **Error Messages**: Don't expose internal proxy details in error messages
5. **Logging**: Log requests for monitoring, but don't log API keys

## Testing

Test cases to verify:
- ✅ Valid video with captions → returns transcript
- ✅ Video without captions → returns `no_captions` error
- ✅ Invalid video ID → returns `invalid_video` error
- ✅ Private video → returns `private` error
- ✅ Proxy failure → retries with next proxy
- ✅ All proxies fail → returns `blocked` error
- ✅ Rate limiting → returns `rate_limited` error
- ✅ Authentication → rejects requests without valid API key

## Future Enhancements

- **Caching**: Cache transcripts for a short period (e.g., 1 hour)
- **Multiple Languages**: Support requesting specific language
- **Batch Requests**: Support multiple video IDs in one request
- **Webhook Support**: Async processing with webhook callbacks
- **Metrics**: Track success rates, proxy performance, etc.

## References

- Current TypeScript implementation: `server/services/sourceContent/youtubeIngest.ts`
- Existing Python proxy system: Your previous Cloudflare project
- YouTube Innertube API: Internal YouTube API (reverse engineered)
