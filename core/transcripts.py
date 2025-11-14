from typing import Optional, Tuple, List, Dict, Any

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from youtube_transcript_api.formatters import TextFormatter

# Note: avoid importing audio_fetch (yt_dlp) at module import time. We'll import lazily in fallback.

try:
    from faster_whisper import WhisperModel  # type: ignore
except Exception:  # pragma: no cover
    WhisperModel = None  # Allow import even if dependency missing


class TranscriptResult(Tuple[bool, Optional[str], Dict[str, Any]]):
    pass


def _join_transcript_chunks(chunks: List[dict]) -> str:
    fmt = TextFormatter()
    return fmt.format_transcript(chunks)


def try_fetch_captions(video_id: str, langs: List[str]) -> Dict[str, Any]:
    try:
        tl = YouTubeTranscriptApi.list_transcripts(video_id)
        # try exact langs first
        for lang in langs:
            try:
                tr = tl.find_transcript([lang])
                text = _join_transcript_chunks(tr.fetch())
                if text and text.strip():
                    return {"success": True, "text": text, "source": "captions", "lang": tr.language_code}
            except Exception:
                continue
        # try translate from a few likely languages to first preferred
        try:
            tr_any = tl.find_transcript(["es", "fr", "de"]).translate(langs[0].split(",")[0] if langs else "en")
            text = _join_transcript_chunks(tr_any.fetch())
            if text and text.strip():
                return {"success": True, "text": text, "source": "captions_translated", "lang": tr_any.language_code}
        except Exception:
            pass
        return {"success": False, "error": "No suitable captions"}
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"caption_error: {e}"}


def transcribe_with_whisper(audio_path: str, model_size: str = "small.en", device: str = "cpu") -> Dict[str, Any]:
    if WhisperModel is None:
        return {"success": False, "error": "faster-whisper not installed"}
    model = WhisperModel(model_size, device=device)
    segments, info = model.transcribe(audio_path, vad_filter=True)
    text_chunks: List[str] = []
    seg_count = 0
    for seg in segments:
        seg_count += 1
        text_chunks.append(seg.text)
    text = " ".join(s.strip() for s in text_chunks if s and s.strip())
    return {
        "success": True,
        "text": text,
        "source": "whisper",
        "lang": getattr(info, "language", None) or "en",
        "segments": seg_count,
    }


def fetch_transcript_with_fallback(video_id: str, langs: List[str], model_size: str, device: str) -> Dict[str, Any]:
    # First try captions
    cap = try_fetch_captions(video_id, langs)
    if cap.get("success") and cap.get("text"):
        return cap
    # Download audio then transcribe
    try:
        from .audio_fetch import download_youtube_audio  # lazy import to avoid hard dep at import time
    except Exception as e:
        return {"success": False, "error": f"yt_dlp_not_available: {e}"}
    audio_path, bytes_downloaded, duration = download_youtube_audio(video_id)
    try:
        asr = transcribe_with_whisper(audio_path, model_size=model_size, device=device)
        if asr.get("success"):
            asr["bytes_downloaded"] = bytes_downloaded
            asr["duration_s"] = duration
            return asr
        return {"success": False, "error": asr.get("error") or "asr_failed", "bytes_downloaded": bytes_downloaded, "duration_s": duration}
    finally:
        # cleanup temp file
        try:
            import os
            tmpdir = os.path.dirname(audio_path)
            if os.path.exists(audio_path):
                os.remove(audio_path)
            # also remove temp dir
            if os.path.isdir(tmpdir):
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
