from typing import Optional, List, Dict, Any
import logging

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from youtube_transcript_api.formatters import TextFormatter

# Note: avoid importing audio_fetch (yt_dlp) at module import time. We'll import lazily in fallback.


def _join_transcript_chunks(chunks: List[dict]) -> str:
    fmt = TextFormatter()
    return fmt.format_transcript(chunks)


def _get_duration_from_yt_dlp(video_id: str) -> Optional[float]:
    """Fetch video duration (seconds) via yt-dlp info-only. Returns None on error."""
    try:
        from yt_dlp import YoutubeDL  # lazy import
        ydl_opts = {
            "quiet": True, 
            "skip_download": True,
            "ignoreerrors": True,  # Continue on download errors to still get metadata
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False, process=False)
            if isinstance(info, dict):
                duration = info.get("duration")
                if duration is not None:
                    return float(duration)
    except Exception:
        logging.getLogger(__name__).debug("yt_dlp_duration_error", exc_info=True)
    return None


def try_fetch_captions(video_id: str, langs: List[str]) -> Dict[str, Any]:
    try:
        api = YouTubeTranscriptApi()
        tl = api.list(video_id)
        # try exact langs first
        for lang in langs:
            try:
                tr = tl.find_transcript([lang])
                text = _join_transcript_chunks(tr.fetch())
                if text and text.strip():
                    duration_s = _get_duration_from_yt_dlp(video_id)
                    return {"success": True, "text": text, "source": "captions", "lang": tr.language_code, "duration_s": duration_s}
            except Exception:
                logging.getLogger(__name__).debug("caption_lang_try_error", exc_info=True)
                continue
        # try translate from a few likely languages to first preferred
        try:
            target_lang = (langs[0].split(",")[0] if (isinstance(langs, list) and len(langs) > 0 and isinstance(langs[0], str)) else "en")
            tr_any = tl.find_transcript(["es", "fr", "de"]).translate(target_lang)
            text = _join_transcript_chunks(tr_any.fetch())
            if text and text.strip():
                duration_s = _get_duration_from_yt_dlp(video_id)
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
