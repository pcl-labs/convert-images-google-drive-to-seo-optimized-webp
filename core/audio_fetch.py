import os
import tempfile
from typing import Tuple, Optional

from yt_dlp import YoutubeDL


def download_youtube_audio(video_id: str, timeout: int = 600) -> Tuple[str, int, Optional[float]]:
    """
    Download audio-only stream for a YouTube video to a temp file.

    Returns:
      (file_path, bytes_downloaded, duration_seconds)
    """
    tmpdir = tempfile.mkdtemp(prefix="yt-audio-")
    # Keep container as provided (webm/m4a); faster-whisper can handle common formats
    outtmpl = os.path.join(tmpdir, f"%(id)s.%(ext)s")

    bytes_downloaded: int = 0
    info_duration: Optional[float] = None

    def _progress_hook(d):
        nonlocal bytes_downloaded
        if d.get("status") == "finished":
            # size may be missing; prefer d["total_bytes"] when available
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if isinstance(total, int):
                bytes_downloaded = total

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "progress_hooks": [_progress_hook],
        # Network robustness
        "socket_timeout": 30,
        "retries": 3,
    }

    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # Choose the actual filename yt-dlp wrote
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            # Some postprocessors can change extension; find the first file in tmpdir
            for fn in os.listdir(tmpdir):
                fp = os.path.join(tmpdir, fn)
                if os.path.isfile(fp):
                    filename = fp
                    break
        # Duration (seconds) if available
        info_duration = info.get("duration") if isinstance(info, dict) else None

    return filename, int(bytes_downloaded or os.path.getsize(filename)), info_duration
