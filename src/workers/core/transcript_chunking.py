from typing import List, Dict, Any

DEFAULT_CHUNK_CHARS = 2000
DEFAULT_OVERLAP_CHARS = 400


def chunk_transcript(
    text: str,
    chunk_chars: int = 2000,
    overlap_chars: int = 400,
) -> List[Dict[str, Any]]:
    """Naive character-based chunking with configurable overlap.

    This keeps implementation simple and deterministic for now; we can
    later replace it with a sentence-aware variant without changing
    callers.
    """
    clean = (text or "").strip()
    if not clean:
        return []
    if chunk_chars <= 0:
        raise ValueError("chunk_chars must be a positive integer")
    if overlap_chars < 0 or overlap_chars >= chunk_chars:
        raise ValueError("overlap_chars must be in the range 0 <= overlap_chars < chunk_chars")

    chunks: List[Dict[str, Any]] = []
    n = len(clean)
    start = 0
    idx = 0

    while start < n:
        end = min(start + chunk_chars, n)
        chunk_text = clean[start:end]
        chunk_text = text[start:end]
        chunks.append(
            {
                "chunk_index": idx,
                "start_char": start,
                "end_char": end,
                "text": chunk_text,
            }
        )
        idx += 1
        if end == n:
            break
        # Overlap with previous chunk to avoid hard boundaries
        if overlap_chars > 0:
            start = max(0, end - overlap_chars)
        else:
            start = end

    return chunks
