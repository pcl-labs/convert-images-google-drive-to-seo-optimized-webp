# YouTube Transcript Retrieval

This directory contains scripts for retrieving transcripts from YouTube videos using multiple methods.

## Overview

The solution provides several approaches to retrieve transcripts from YouTube videos, with fallback mechanisms to handle YouTube's restrictions:

1. **youtube-transcript-api**: Uses the `youtube_transcript_api` library with proxy support
2. **YouTube Data API**: Uses the official YouTube Data API (requires API key)
3. **yt-dlp**: Uses the `yt-dlp` library, which is more resilient to YouTube's restrictions

## Installation

### Prerequisites

- Python 3.6+
- Required Python packages:
  ```
  pip install youtube-transcript-api requests
  ```

### Optional Dependencies

- **YouTube Data API**: Requires a YouTube API key
  - Set the `YOUTUBE_API_KEY` environment variable
  - Get an API key from [Google Cloud Console](https://console.cloud.google.com/)

- **yt-dlp**: For the most reliable method
  ```
  pip install yt-dlp
  ```

## Usage

### Basic Usage

```bash
python3 unified_transcript.py <video_id>
```

Example:
```bash
python3 unified_transcript.py dQw4w9WgXcQ
```

### Advanced Options

```bash
python3 unified_transcript.py <video_id> [--no-api] [--no-proxy] [--no-yt-dlp]
```

- `--no-api`: Skip using the YouTube Data API
- `--no-proxy`: Skip using proxy-based methods
- `--no-yt-dlp`: Skip using yt-dlp

### Individual Methods

You can also use each method individually:

```bash
# Using youtube-transcript-api with proxy support
python3 get_transcript.py <video_id>

# Using YouTube Data API
python3 youtube_api_transcript.py <video_id>

# Using yt-dlp
python3 youtube_dl_transcript.py <video_id>
```

## Proxy Management

The solution includes a proxy management system to handle YouTube's IP restrictions:

```bash
# Find and validate proxies
python3 find_proxies.py

# Test the proxy manager
python3 proxy_manager.py
```

## Troubleshooting

### YouTube is blocking requests

If you encounter IP blocking from YouTube:

1. Try using the yt-dlp method, which is most resilient
2. Set up a YouTube API key for the YouTube Data API method
3. Use the proxy management system to find working proxies

### Missing Dependencies

If you see errors about missing dependencies:

```
pip install youtube-transcript-api requests yt-dlp
```

## License

This project is licensed under the MIT License - see the LICENSE file for details. 