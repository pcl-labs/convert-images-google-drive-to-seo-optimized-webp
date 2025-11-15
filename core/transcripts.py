from typing import Optional, List, Dict, Any
import logging

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from youtube_transcript_api.formatters import TextFormatter


def _join_transcript_chunks(chunks: List[dict]) -> str:
    fmt = TextFormatter()
    return fmt.format_transcript(chunks)


def _get_duration_from_chunks(chunks) -> Optional[float]:
    """Calculate video duration from transcript chunks. Returns None if chunks are empty or invalid.
    
    Chunks are FetchedTranscriptSnippet objects with .start and .duration attributes.
    """
    if not chunks:
        return None
    try:
        # Get the last chunk's start time + duration
        last_chunk = chunks[-1]
        start = getattr(last_chunk, "start", None)
        duration = getattr(last_chunk, "duration", None)
        if start is not None and duration is not None:
            return float(start) + float(duration)
    except (AttributeError, ValueError, TypeError):
        pass
    return None


def try_fetch_captions(video_id: str, langs: List[str]) -> Dict[str, Any]:
    try:
        api = YouTubeTranscriptApi()
        tl = api.list(video_id)
        # try exact langs first
        for lang in langs:
            try:
                tr = tl.find_transcript([lang])
                chunks = tr.fetch()
                text = _join_transcript_chunks(chunks)
                if text and text.strip():
                    # Extract duration from transcript chunks
                    duration_s = _get_duration_from_chunks(chunks)
                    return {"success": True, "text": text, "source": "captions", "lang": tr.language_code, "duration_s": duration_s}
            except Exception:
                logging.getLogger(__name__).debug("caption_lang_try_error", exc_info=True)
                continue
        # try translate from a few likely languages to first preferred
        try:
            target_lang = (langs[0].split(",")[0] if (isinstance(langs, list) and len(langs) > 0 and isinstance(langs[0], str)) else "en")
            tr_any = tl.find_transcript(["es", "fr", "de"]).translate(target_lang)
            chunks = tr_any.fetch()
            text = _join_transcript_chunks(chunks)
            if text and text.strip():
                # Extract duration from transcript chunks
                duration_s = _get_duration_from_chunks(chunks)
                return {"success": True, "text": text, "source": "captions_translated", "lang": tr_any.language_code, "duration_s": duration_s}
        except Exception:
            logging.getLogger(__name__).debug("caption_translate_error", exc_info=True)
        return {"success": False, "error": "No suitable captions"}
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"caption_error: {e}"}


def fetch_transcript_with_fallback(video_id: str, langs: List[str]) -> Dict[str, Any]:
    """Captions-only transcript fetch. Returns error if captions unavailable."""
    cap = try_fetch_captions(video_id, langs)
    if cap.get("success") and cap.get("text"):
        return cap
    return {"success": False, "error": cap.get("error") or "captions_unavailable"}
