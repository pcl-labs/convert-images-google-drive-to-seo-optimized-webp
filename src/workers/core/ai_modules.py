from __future__ import annotations

from typing import Dict, Any, List, Optional
import textwrap
import re
import html
import logging
import os
import json

try:
    from openai import AsyncOpenAI, OpenAIError  # type: ignore
except Exception:  # pragma: no cover - OpenAI optional during tests
    AsyncOpenAI = None  # type: ignore
    OpenAIError = Exception  # type: ignore

from src.workers.api.config import settings

logger = logging.getLogger(__name__)


def _coerce_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return str(text).strip()


def generate_outline(text: str, max_sections: int = 5) -> List[Dict[str, str]]:
    """Naive outline generator that splits text into paragraph chunks."""
    clean = _coerce_text(text)
    if not clean:
        return []
    # Ensure positive section count
    try:
        max_sections = int(max_sections)
    except (ValueError, TypeError):
        max_sections = 5
    if max_sections < 1:
        max_sections = 1
    paragraphs = [p.strip() for p in clean.split("\n") if p.strip()]
    if not paragraphs:
        paragraphs = [clean]
    chunk_size = max(1, len(paragraphs) // max_sections)
    outline = []
    for idx in range(0, len(paragraphs), chunk_size):
        chunk = paragraphs[idx : idx + chunk_size]
        title = chunk[0][:80]
        summary = textwrap.shorten(" ".join(chunk), width=280, placeholder="…")
        outline.append({"title": title or f"Section {len(outline)+1}", "summary": summary})
        if len(outline) >= max_sections:
            break
    return outline


def _split_sentences(text: str) -> List[str]:
    """
    Robust sentence splitter that protects common non-sentence-ending patterns.
    Masks abbreviations, decimals, URLs, ellipses, emails, and IPs before splitting.
    """
    if not text:
        return []
    
    # Dictionary to store masked patterns: (start_pos, end_pos) -> placeholder
    # We'll collect all matches first, then replace in reverse order to preserve positions
    matches: List[tuple[int, int, str]] = []  # (start, end, pattern_text)
    placeholder_counter = 0
    
    def create_placeholder() -> str:
        nonlocal placeholder_counter
        placeholder = f"__PLACEHOLDER_{placeholder_counter}__"
        placeholder_counter += 1
        return placeholder
    
    # Pattern 1: URLs (http://, https://, www.)
    url_pattern = re.compile(
        r'(https?://[^\s]+|www\.[^\s]+|[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?)',
        re.IGNORECASE
    )
    # Trailing punctuation characters to trim (sentence-ending punctuation)
    # Exclude URL-valid punctuation like ? # & = / which are part of URL structure
    trailing_punctuation = '.,:;!?)]}\'"'
    
    for match in url_pattern.finditer(text):
        url_text = match.group(0)
        start_pos = match.start()
        end_pos = match.end()
        
        # Trim trailing punctuation, but preserve valid URL structure
        # Valid URL punctuation includes: / ? # & = % + - _ ~ @ :
        # We'll trim sentence-ending punctuation from the end
        cleaned_url = url_text
        original_length = len(url_text)
        
        # Trim trailing punctuation characters one by one from the end
        # Stop if we encounter a character that's part of URL structure
        while cleaned_url:
            last_char = cleaned_url[-1]
            # Stop trimming if we hit a valid URL structure character
            if last_char in '/?#&=%+-_~@:':
                break
            # Stop trimming if we hit alphanumeric or other non-punctuation
            if last_char.isalnum() or last_char not in trailing_punctuation:
                break
            # Trim this trailing punctuation character
            cleaned_url = cleaned_url[:-1]
        
        # Update end position if we trimmed characters
        trimmed_count = original_length - len(cleaned_url)
        if trimmed_count > 0:
            end_pos = start_pos + len(cleaned_url)
        
        matches.append((start_pos, end_pos, cleaned_url))
    
    # Pattern 2: Email addresses
    email_pattern = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
    for match in email_pattern.finditer(text):
        matches.append((match.start(), match.end(), match.group(0)))
    
    # Pattern 3: IP addresses
    ip_pattern = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
    for match in ip_pattern.finditer(text):
        matches.append((match.start(), match.end(), match.group(0)))
    
    # Pattern 4: Decimal numbers (including percentages and version numbers)
    decimal_pattern = re.compile(r'\b\d+\.\d+(?:%|st|nd|rd|th)?\b', re.IGNORECASE)
    for match in decimal_pattern.finditer(text):
        matches.append((match.start(), match.end(), match.group(0)))
    
    # Pattern 5: Ellipses (both ... and …)
    ellipsis_pattern = re.compile(r'\.{2,}|…')
    for match in ellipsis_pattern.finditer(text):
        matches.append((match.start(), match.end(), match.group(0)))
    
    # Pattern 6: Common abbreviations (case-insensitive, word boundaries)
    abbreviations = [
        r'\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|Esq|Rev|Hon|Capt|Col|Gen|Lt|Sgt|Cpl|Pvt)\.',
        r'\b(?:etc|vs|viz|i\.e|e\.g|cf|ex|inc|corp|ltd|co|llc|llp)\.',
        r'\b(?:am|pm|a\.m|p\.m)\.',
        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.',
        r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.',
        r'\b(?:No|no)\.',
        r'\b(?:pp|pg|vol|ch|sec|fig|eq|ref|refs)\.',
        r'\b(?:ed|eds|trans|vol|vols|pp|pg|pgs)\.',
    ]
    for abbrev_pattern in abbreviations:
        abbrev_re = re.compile(abbrev_pattern, re.IGNORECASE)
        for match in abbrev_re.finditer(text):
            matches.append((match.start(), match.end(), match.group(0)))
    
    # Pattern 7: Single letter abbreviations (e.g., "A.", "B.", "X.", "a.", "i.")
    single_letter_pattern = re.compile(r'\b[A-Za-z]\.')
    for match in single_letter_pattern.finditer(text):
        # Only mask if followed by space or end of string (not another letter)
        pos = match.end()
        if pos >= len(text) or text[pos].isspace():
            matches.append((match.start(), match.end(), match.group(0)))
    
    # Remove overlapping matches (keep the first/longest one)
    # Sort by start position, then by length (descending)
    matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    non_overlapping: List[tuple[int, int, str]] = []
    for start, end, pattern_text in matches:
        if not non_overlapping or start >= non_overlapping[-1][1]:
            non_overlapping.append((start, end, pattern_text))
    
    # Replace matches in reverse order to preserve positions
    placeholders: Dict[str, str] = {}
    for start, end, pattern_text in reversed(non_overlapping):
        placeholder = create_placeholder()
        placeholders[placeholder] = pattern_text
        text = text[:start] + placeholder + text[end:]
    
    # Now split on sentence boundaries: . ! ? followed by space or end of string
    # Also handle cases where punctuation is followed by quote marks
    # Pattern matches sentence-ending punctuation followed by whitespace, quotes, or end of string
    sentence_end_pattern = re.compile(r'([.!?]+)(?:\s+|["\']\s*|$)')
    
    sentences: List[str] = []
    last_end = 0
    
    for match in sentence_end_pattern.finditer(text):
        # Get the position where the sentence ends (including punctuation)
        end_pos = match.end()
        sentence = text[last_end:end_pos].strip()
        if sentence:
            sentences.append(sentence)
        last_end = end_pos
    
    # Add any remaining text
    remaining = text[last_end:].strip()
    if remaining:
        sentences.append(remaining)
    
    # Restore placeholders
    result: List[str] = []
    for sentence in sentences:
        for placeholder, original in placeholders.items():
            sentence = sentence.replace(placeholder, original)
        if sentence.strip():
            result.append(sentence.strip())
    
    return result


def organize_chapters(text: str, target_chapters: int = 4) -> List[Dict[str, str]]:
    """Split text into pseudo chapters."""
    clean = _coerce_text(text)
    if not clean:
        return []
    # Ensure positive chapters target
    try:
        target_chapters = int(target_chapters)
    except (ValueError, TypeError):
        target_chapters = 4
    if target_chapters <= 0:
        target_chapters = 1
    sentences = _split_sentences(clean)
    per_chapter = max(1, len(sentences) // target_chapters)
    chapters: List[Dict[str, str]] = []
    for idx in range(0, len(sentences), per_chapter):
        chunk = sentences[idx : idx + per_chapter]
        if not chunk:
            continue
        title = chunk[0][:70]
        summary = textwrap.shorten(" ".join(chunk), width=360, placeholder="…")
        chapters.append({"title": title or f"Chapter {len(chapters)+1}", "summary": summary})
        if len(chapters) >= target_chapters:
            break
    return chapters


def _compose_blog_stub(
    chapters: List[Dict[str, str]],
    tone: str = "informative",
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """Fallback blog composer that mirrors old behavior (deterministic + offline)."""
    if not chapters:
        return {
            "markdown": "",
            "meta": {
                "tone": tone,
                "word_count": 0,
                "engine": "stub",
                "model": model or "stub",
                "temperature": temperature,
            },
        }
    parts = []
    for chap in chapters:
        title = chap.get("title") or "Section"
        summary = chap.get("summary") or ""
        parts.append(f"## {title}\n\n{summary}\n")
    markdown = "\n".join(parts).strip()
    word_count = len(markdown.split())
    return {
        "markdown": markdown,
        "meta": {
            "tone": tone,
            "sections": len(chapters),
            "word_count": word_count,
            "engine": "stub",
            "model": model or "stub",
            "temperature": temperature,
        },
    }


def default_title_from_outline(outline: List[Dict[str, str]]) -> str:
    if not outline:
        return "Untitled Draft"
    for item in outline:
        title = (item or {}).get("title")
        if title:
            return title[:120]
    return "Untitled Draft"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    cleaned = _SLUG_RE.sub("-", text.lower()).strip("-")
    return cleaned or "post"


def generate_seo_metadata(text: str, outline: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Return lightweight SEO metadata derived from text + outline."""
    base_text = _coerce_text(text)
    title = default_title_from_outline(outline or []) if outline else None
    if not title:
        title = textwrap.shorten(base_text, width=60, placeholder="…") or "Untitled Draft"
    description = textwrap.shorten(base_text, width=180, placeholder="…") if base_text else title
    words = re.findall(r"[a-z0-9]{4,}", base_text.lower())
    keywords: List[str] = []
    for word in words:
        if word not in keywords:
            keywords.append(word)
        if len(keywords) >= 10:
            break
    return {
        "title": title,
        "description": description,
        "slug": _slugify(title),
        "keywords": keywords,
        "hero_image": None,
    }


def generate_image_prompts(chapters: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Produce simple illustrative prompts per chapter for downstream image generation."""
    prompts: List[Dict[str, Any]] = []
    for idx, chapter in enumerate(chapters):
        title = (chapter or {}).get("title") or f"Section {idx + 1}"
        summary = (chapter or {}).get("summary") or ""
        prompts.append(
            {
                "chapter_index": idx,
                "title": title,
                "prompt": f"Create a cinematic wide shot illustrating '{title}' focusing on {summary[:140]}",
                "style": "cinematic",
                "aspect_ratio": "16:9",
            }
        )
    return prompts


def markdown_to_html(markdown_text: str) -> str:
    """Very small markdown-to-HTML converter for headings + paragraphs."""
    lines: List[str] = []
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("### "):
            lines.append(f"<h3>{html.escape(line[4:].strip())}</h3>")
        elif line.startswith("## "):
            lines.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        elif line.startswith("# "):
            lines.append(f"<h1>{html.escape(line[2:].strip())}</h1>")
        else:
            lines.append(f"<p>{html.escape(line)}</p>")
    return "\n".join(lines)


_openai_client: Optional[AsyncOpenAI] = None


def _should_use_openai() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    if not settings.openai_api_key:
        return False
    if AsyncOpenAI is None:
        logger.debug("OpenAI client unavailable; falling back to stub composer")
        return False
    return True


def _format_chapters_for_prompt(chapters: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for idx, chap in enumerate(chapters, start=1):
        title = (chap or {}).get("title") or f"Section {idx}"
        summary = (chap or {}).get("summary") or ""
        lines.append(f"{idx}. {title.strip()}\n   Summary: {summary.strip()}")
    return "\n".join(lines)


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for AI blog generation")
    if AsyncOpenAI is None:
        raise RuntimeError("openai package is not installed")
    if _openai_client is None:
        client_kwargs: Dict[str, Any] = {"api_key": settings.openai_api_key}
        if settings.openai_api_base:
            client_kwargs["base_url"] = settings.openai_api_base
        _openai_client = AsyncOpenAI(**client_kwargs)
    return _openai_client


async def compose_blog(
    chapters: List[Dict[str, str]],
    tone: str = "informative",
    seo_metadata: Optional[Dict[str, Any]] = None,
    extra_context: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Compose a long-form markdown blog post from structured chapters using OpenAI.
    Falls back to the deterministic stub when OpenAI is unavailable (e.g., tests/offline).
    """
    seo_metadata = seo_metadata or {}
    extra_context = extra_context or {}
    if not chapters:
        return _compose_blog_stub(chapters, tone=tone, model=model, temperature=temperature)

    if not _should_use_openai():
        return _compose_blog_stub(chapters, tone=tone, model=model, temperature=temperature)

    prompt_outline = _format_chapters_for_prompt(chapters)
    keywords = ", ".join(seo_metadata.get("keywords", []))
    system_prompt = (
        "You are Quill's senior marketing copywriter. "
        "Given a structured outline plus SEO metadata, craft a comprehensive, engaging, "
        "and factually consistent blog article. Always write in Markdown with a single H1 title "
        "followed by well-structured H2/H3 sections, short paragraphs, scannable bullet lists, "
        "and a concluding call-to-action."
    )
    requirements = [
        f"Tone: {tone}",
        f"Primary title hint: {seo_metadata.get('title') or ''}",
        f"Meta description guidance: {seo_metadata.get('description') or ''}",
        f"Target keywords: {keywords or 'use best-fit based on outline'}",
        "Length: 900-1200 words unless outline implies otherwise.",
        "Add SEO-friendly subheadings, numbered/bullet lists where useful, and contextual transitions between sections.",
        "Do not include markdown frontmatter or HTML—return pure Markdown body.",
        "Keep factual claims grounded in provided outline; do not invent statistics.",
        "Close with a concise CTA tailored to the topic.",
    ]
    if extra_context:
        requirements.append(f"Additional context: {json.dumps(extra_context, default=str)[:800]}")

    user_prompt = (
        "Generate a publication-ready blog article.\n\n"
        "Outline:\n"
        f"{prompt_outline}\n\n"
        "Requirements:\n- " + "\n- ".join(req for req in requirements if req.strip())
    )

    try:
        client = _get_openai_client()
    except Exception:
        logger.warning("openai_client_unavailable", exc_info=True)
        return _compose_blog_stub(chapters, tone=tone, model=model, temperature=temperature)

    model_name = (model or settings.openai_blog_model or "gpt-5.1").strip()
    temp_value = temperature if temperature is not None else settings.openai_blog_temperature
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temp_value,
            max_tokens=settings.openai_blog_max_output_tokens,
        )
    except OpenAIError as exc:
        logger.error(
            "openai_compose_blog_failed",
            exc_info=True,
            extra={"model": model_name, "reason": getattr(exc, "message", str(exc))},
        )
        return _compose_blog_stub(chapters, tone=tone, model=model_name, temperature=temp_value)
    except Exception as exc:  # pragma: no cover - network/runtime edge cases
        logger.error("openai_compose_blog_unexpected", exc_info=True, extra={"model": model_name})
        return _compose_blog_stub(chapters, tone=tone, model=model_name, temperature=temp_value)

    # Safely extract markdown from standard OpenAI chat/completion response shapes
    output_text: str = ""

    if response is not None:
        choices = getattr(response, "choices", None)
        if isinstance(choices, list) and choices:
            first = choices[0]
            content: Any = None

            # Chat-style response: choice.message.content
            message = getattr(first, "message", None)
            if message is not None and hasattr(message, "content"):
                content = getattr(message, "content")

            # Legacy completion: choice.text
            if content is None and hasattr(first, "text"):
                content = getattr(first, "text")

            # Normalize various content shapes into a single string
            if isinstance(content, str):
                output_text = content
            elif isinstance(content, list):
                parts: List[str] = []
                for item in content:
                    if isinstance(item, str):
                        parts.append(item)
                    else:
                        parts.append(str(item))
                output_text = "\n".join(p for p in parts if p.strip())
            elif isinstance(content, (dict, int, float, bool)):
                output_text = str(content)

    # Fallback: if nothing extracted, try any remaining best-effort attributes
    if not output_text and response is not None:
        candidate = getattr(response, "output_text", None)
        if isinstance(candidate, list):
            output_text = "\n".join(str(item) for item in candidate if str(item).strip())
        elif isinstance(candidate, str):
            output_text = candidate

    # Final fallback: mirror legacy block-iterating behavior if still empty
    if not output_text and response is not None:
        output_chunks: List[str] = []
        for item in getattr(response, "output", []) or []:
            contents = getattr(item, "content", None)
            if not contents:
                continue
            for block in contents:
                text_obj = getattr(block, "text", None)
                if hasattr(text_obj, "value"):
                    output_chunks.append(str(text_obj.value))
                elif isinstance(text_obj, str):
                    output_chunks.append(text_obj)
        if output_chunks:
            output_text = "\n".join(output_chunks)

    markdown = (output_text or "").strip()
    if not markdown:
        return _compose_blog_stub(chapters, tone=tone, model=model_name, temperature=temp_value)

    word_count = len(markdown.split())
    return {
        "markdown": markdown,
        "meta": {
            "tone": tone,
            "sections": len(chapters),
            "word_count": word_count,
            "engine": "openai",
            "model": model_name,
            "temperature": temp_value,
        },
    }
