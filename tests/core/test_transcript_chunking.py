from src.workers.core.transcript_chunking import chunk_transcript, DEFAULT_CHUNK_CHARS, DEFAULT_OVERLAP_CHARS


def test_chunk_transcript_basic_splitting():
    text = "a" * (DEFAULT_CHUNK_CHARS + 100)
    chunks = chunk_transcript(text)
    # Should produce two chunks with overlap
    assert len(chunks) == 2
    assert chunks[0]["chunk_index"] == 0
    assert chunks[1]["chunk_index"] == 1
    assert chunks[0]["start_char"] == 0
    assert chunks[0]["end_char"] == DEFAULT_CHUNK_CHARS
    assert chunks[1]["start_char"] == max(0, DEFAULT_CHUNK_CHARS - DEFAULT_OVERLAP_CHARS)
    assert chunks[1]["end_char"] == len(text)


def test_chunk_transcript_empty_text():
    assert chunk_transcript("") == []


def test_chunk_transcript_small_text_single_chunk():
    text = "hello world"
    chunks = chunk_transcript(text, chunk_chars=50, overlap_chars=0)
    assert len(chunks) == 1
    c = chunks[0]
    assert c["chunk_index"] == 0
    assert c["start_char"] == 0
    assert c["end_char"] == len(text)
    assert c["text"] == text
