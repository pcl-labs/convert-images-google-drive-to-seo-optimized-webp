"""
YouTube transcript retrieval module.
"""

import json
from typing import Dict, Optional
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from youtube_transcript_api.formatters import TextFormatter
from .logger import setup_logger

logger = setup_logger()

class TranscriptError(Exception):
    """Base exception for transcript retrieval errors."""
    pass

class NoTranscriptAvailableError(TranscriptError):
    """Exception raised when no transcript is available."""
    pass

class VideoUnavailableError(TranscriptError):
    """Exception raised when video is unavailable."""
    pass

def get_transcript(video_id: str, use_proxy: bool = False) -> Dict:
    """
    Get transcript using youtube-transcript-api with fallback options
    
    Args:
        video_id: YouTube video ID
        use_proxy: Whether to use proxy
    
    Returns:
        Dict with transcript or error message
    
    Raises:
        NoTranscriptAvailableError: When no suitable transcript is found
        VideoUnavailableError: When video is unavailable
        TranscriptError: For other transcript-related errors
    """
    logger.info(f"Retrieving transcript for video ID: {video_id}")
    
    try:
        # Configure proxy if needed
        if use_proxy:
            logger.debug("Proxy configuration enabled")
            # YouTubeTranscriptApi.set_proxy('http://your-proxy:port')
        
        # Get available transcripts
        logger.debug("Fetching available transcripts...")
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        # Try to get English transcript with fallbacks
        try:
            logger.debug("Attempting to get English transcript...")
            transcript = transcript_list.find_transcript(['en'])
            logger.info("Found English transcript")
        except:
            try:
                logger.debug("No English transcript found, trying auto-generated English...")
                transcript = transcript_list.find_transcript(['en-US', 'en-GB'])
                logger.info("Found auto-generated English transcript")
            except:
                try:
                    logger.debug("No English transcript found, trying to translate from other languages...")
                    transcript = transcript_list.find_transcript(['es', 'fr', 'de']).translate('en')
                    logger.info("Found and translated transcript to English")
                except:
                    error_msg = "No suitable transcript found"
                    logger.error(error_msg)
                    raise NoTranscriptAvailableError(error_msg)
        
        # Format the transcript
        logger.debug("Formatting transcript...")
        formatter = TextFormatter()
        transcript_text = formatter.format_transcript(transcript.fetch())
        
        if not transcript_text:
            error_msg = "Transcript text is empty"
            logger.error(error_msg)
            raise NoTranscriptAvailableError(error_msg)
        
        logger.info("Successfully retrieved transcript")
        return {
            "success": True,
            "transcript": transcript_text,
            "source": "youtube-transcript-api"
        }
    
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        logger.error(f"Transcript error: {str(e)}")
        raise NoTranscriptAvailableError(str(e))
    except VideoUnavailable as e:
        logger.error(f"Video unavailable: {str(e)}")
        raise VideoUnavailableError("Video is unavailable. It may be private, unlisted, or deleted.")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise TranscriptError(f"Unexpected error: {str(e)}") 