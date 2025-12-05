#!/usr/bin/env python3
import sys
import json
import argparse
from typing import Dict, Optional
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from youtube_transcript_api.formatters import TextFormatter

def get_transcript(video_id: str, use_proxy: bool = False) -> Dict:
    """
    Get transcript using youtube-transcript-api with fallback options
    
    Args:
        video_id: YouTube video ID
        use_proxy: Whether to use proxy
    
    Returns:
        Dict with transcript or error message
    """
    try:
        # Configure proxy if needed
        if use_proxy:
            # YouTubeTranscriptApi.set_proxy('http://your-proxy:port')
            pass
        
        # Get available transcripts
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        # Try to get English transcript with fallbacks
        try:
            # First try: English
            transcript = transcript_list.find_transcript(['en'])
        except:
            try:
                # Second try: Auto-generated English
                transcript = transcript_list.find_transcript(['en-US', 'en-GB'])
            except:
                try:
                    # Third try: Translate from other languages
                    transcript = transcript_list.find_transcript(['es', 'fr', 'de']).translate('en')
                except:
                    return {
                        "success": False,
                        "error": "No suitable transcript found",
                        "source": "youtube-transcript-api"
                    }
        
        # Format the transcript
        formatter = TextFormatter()
        transcript_text = formatter.format_transcript(transcript.fetch())
        
        if not transcript_text:
            return {
                "success": False,
                "error": "Transcript text is empty",
                "source": "youtube-transcript-api"
            }
        
        return {
            "success": True,
            "transcript": transcript_text,
            "source": "youtube-transcript-api"
        }
    
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        return {
            "success": False,
            "error": str(e),
            "source": "youtube-transcript-api"
        }
    except VideoUnavailable as e:
        return {
            "success": False,
            "error": "Video is unavailable. It may be private, unlisted, or deleted.",
            "source": "youtube-transcript-api"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "source": "youtube-transcript-api"
        }

def main():
    parser = argparse.ArgumentParser(description="Get YouTube video transcript")
    parser.add_argument("video_id", help="YouTube video ID")
    parser.add_argument("--use-proxy", action="store_true", help="Use proxy for transcript retrieval")
    
    args = parser.parse_args()
    
    result = get_transcript(args.video_id, use_proxy=args.use_proxy)
    print(json.dumps(result))

if __name__ == "__main__":
    main() 