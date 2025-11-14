import os
from core.transcripts import fetch_transcript_with_fallback


def test_fetch_transcript_with_duration():
    """Test that fetch_transcript_with_fallback returns duration_s when successful."""
    video_id = os.getenv("TEST_YOUTUBE_VIDEO_ID", "AWHeCwChUtE")
    langs = ["en", "en-US", "en-GB"]
    
    result = fetch_transcript_with_fallback(video_id, langs)
    
    assert result.get("success") is True, f"Expected success=True, got {result.get('success')}"
    assert result.get("text") is not None, "Expected text to be present"
    assert len(result.get("text", "").strip()) > 0, "Expected non-empty text"
    assert result.get("duration_s") is not None, f"Expected duration_s to be present, got None. Full result: {result}"
    assert isinstance(result.get("duration_s"), (int, float)), f"Expected duration_s to be numeric, got {type(result.get('duration_s'))}"
    assert result.get("duration_s") > 0, f"Expected duration_s > 0, got {result.get('duration_s')}"
    assert result.get("source") in ["captions", "captions_translated"], f"Expected source to be captions, got {result.get('source')}"
    assert result.get("lang") is not None, "Expected lang to be present"

