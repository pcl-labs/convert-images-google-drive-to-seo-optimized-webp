"""
Command-line interface for YouTube transcript retrieval.
"""

import sys
import json
import argparse
from .transcript import get_transcript, TranscriptError, NoTranscriptAvailableError, VideoUnavailableError
from .logger import setup_logger

logger = setup_logger()

def main():
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(description="Get YouTube video transcript")
    parser.add_argument("video_id", help="YouTube video ID")
    parser.add_argument("--use-proxy", action="store_true", help="Use proxy for transcript retrieval")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    
    args = parser.parse_args()
    
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    try:
        result = get_transcript(args.video_id, use_proxy=args.use_proxy)
        print(json.dumps(result))
    except NoTranscriptAvailableError as e:
        result = {
            "success": False,
            "error": str(e),
            "source": "youtube-transcript-api"
        }
        print(json.dumps(result))
        sys.exit(1)
    except VideoUnavailableError as e:
        result = {
            "success": False,
            "error": str(e),
            "source": "youtube-transcript-api"
        }
        print(json.dumps(result))
        sys.exit(1)
    except TranscriptError as e:
        result = {
            "success": False,
            "error": str(e),
            "source": "youtube-transcript-api"
        }
        print(json.dumps(result))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        result = {
            "success": False,
            "error": "An unexpected error occurred",
            "source": "youtube-transcript-api"
        }
        print(json.dumps(result))
        sys.exit(1)

if __name__ == "__main__":
    main() 