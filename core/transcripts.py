from typing import Optional, List, Dict, Any

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from youtube_transcript_api.formatters import TextFormatter

# Note: avoid importing audio_fetch (yt_dlp) at module import time. We'll import lazily in fallback.


def _join_transcript_chunks(chunks: List[dict]) -> str:
    fmt = TextFormatter()
    return fmt.format_transcript(chunks)


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
                    # Try to fetch duration via yt-dlp info-only
                    duration_s = None
                    try:
                        from yt_dlp import YoutubeDL  # lazy import
                        ydl_opts = {"quiet": True, "skip_download": True}
                        with YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                            if isinstance(info, dict):
                                duration_s = info.get("duration")
                    except Exception:
                        duration_s = None
                    return {"success": True, "text": text, "source": "captions", "lang": tr.language_code, "duration_s": duration_s}
            except Exception:
                continue
        # try translate from a few likely languages to first preferred
        try:
            tr_any = tl.find_transcript(["es", "fr", "de"]).translate(langs[0].split(",")[0] if langs else "en")
            text = _join_transcript_chunks(tr_any.fetch())
            if text and text.strip():
                duration_s = None
                try:
                    from yt_dlp import YoutubeDL  # lazy import
                    ydl_opts = {"quiet": True, "skip_download": True}
                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                        if isinstance(info, dict):
                            duration_s = info.get("duration")
                except Exception:
                    duration_s = None
                return {"success": True, "text": text, "source": "captions_translated", "lang": tr_any.language_code, "duration_s": duration_s}
        except Exception:
            pass
        return {"success": False, "error": "No suitable captions"}
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"caption_error: {e}"}


def fetch_transcript_with_fallback(video_id: str, langs: List[str], model_size: str, device: str) -> Dict[str, Any]:
    """Captions-only transcript fetch. Returns error if captions unavailable."""
    cap = try_fetch_captions(video_id, langs)
    if cap.get("success") and cap.get("text"):
        return cap
    return {"success": False, "error": cap.get("error") or "captions_unavailable"}
