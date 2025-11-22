from __future__ import annotations

from typing import Dict, Any, List, Optional
import textwrap
import re
import html
import logging
import os
import json
from urllib.parse import urlparse

from api.config import settings
from api.simple_http import AsyncSimpleClient, HTTPStatusError, RequestError

logger = logging.getLogger(__name__)

_OUTLINE_STOPWORDS = {
    "the",
    "and",
    "that",
    "with",
    "from",
    "your",
    "this",
    "about",
    "what",
    "will",
    "into",
    "have",
    "when",
    "they",
    "them",
    "for",
    "you",
    "are",
    "was",
    "were",
    "their",
    "then",
    "than",
    "over",
}
_KEYWORD_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9']+")


def _coerce_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return str(text).strip()


def _extract_keywords(text: str, limit: int = 5) -> List[str]:
    """Return deterministic keyword list for a block of text."""
    counts: Dict[str, int] = {}
    for word in _KEYWORD_WORD_RE.findall(str(text or "").lower()):
        if len(word) < 4 or word in _OUTLINE_STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    if not counts:
        return []
    sorted_words = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    keywords: List[str] = []
    for word, _ in sorted_words:
        if word in keywords:
            continue
        keywords.append(word)
        if len(keywords) >= limit:
            break
    return keywords


def _first_sentence(text: str) -> str:
    sentences = _split_sentences(text)
    if sentences:
        return sentences[0]
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0]


def _outline_section(
    title: str,
    summary: str,
    slot: str,
    *,
    keywords: Optional[List[str]] = None,
    source: str = "transcript",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    section = {
        "title": title.strip()[:160] or "Section",
        "summary": summary.strip(),
        "slot": slot,
        "keywords": (keywords or [])[:8],
        "source": source,
    }
    if extra:
        section.update(extra)
    return section


def generate_outline(text: str, max_sections: int = 5) -> List[Dict[str, Any]]:
    """Generate a structured outline with intro/body/cta slots."""
    clean = _coerce_text(text)
    if not clean:
        return []
    try:
        max_sections = int(max_sections)
    except (ValueError, TypeError):
        max_sections = 5
    if max_sections < 1:
        max_sections = 1
    max_sections = min(max_sections, 12)
    paragraphs = [p.strip() for p in clean.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [clean]

    outline: List[Dict[str, Any]] = []
    intro_text = paragraphs[0]
    intro_title = _first_sentence(intro_text) or "Introduction"
    intro_summary = textwrap.shorten(intro_text, width=280, placeholder="…")
    outline.append(
        _outline_section(
            intro_title,
            intro_summary,
            "intro",
            keywords=_extract_keywords(intro_text, limit=4),
        )
    )
    if max_sections == 1:
        return outline

    body_slots = max(0, max_sections - 2)
    body_text = "\n\n".join(paragraphs[1:]) if len(paragraphs) > 1 else clean
    body_sentences = _split_sentences(body_text)
    if body_slots and body_sentences:
        chunk_size = max(1, len(body_sentences) // body_slots)
        start = 0
        slot_index = 0
        while start < len(body_sentences) and slot_index < body_slots:
            chunk = body_sentences[start : start + chunk_size]
            if not chunk:
                break
            section_text = " ".join(chunk)
            section_title = _first_sentence(section_text) or f"Section {slot_index + 1}"
            summary = textwrap.shorten(section_text, width=300, placeholder="…")
            outline.append(
                _outline_section(
                    section_title,
                    summary,
                    "body",
                    keywords=_extract_keywords(section_text, limit=4),
                )
            )
            slot_index += 1
            start += chunk_size
            if len(outline) >= max_sections - 1:
                break

    if len(outline) < max_sections:
        cta_summary = "Summarize the key takeaways and invite the reader to take the next action."
        outline.append(
            _outline_section(
                "Call to action",
                cta_summary,
                "cta",
                keywords=["cta", "next steps"],
            )
        )
    return outline[:max_sections]


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


def organize_chapters(text: str, target_chapters: int = 4) -> List[Dict[str, Any]]:
    """Split text into pseudo chapters with intro/body/cta hints."""
    clean = _coerce_text(text)
    if not clean:
        return []
    try:
        target_chapters = int(target_chapters)
    except (ValueError, TypeError):
        target_chapters = 4
    if target_chapters <= 0:
        target_chapters = 1
    target_chapters = min(target_chapters, 12)
    sentences = _split_sentences(clean)
    if not sentences:
        sentences = [clean]
    per_chapter = max(1, len(sentences) // target_chapters)
    chapters: List[Dict[str, Any]] = []
    start = 0
    index = 0
    while start < len(sentences) and index < target_chapters:
        chunk = sentences[start : start + per_chapter]
        if not chunk:
            break
        chunk_text = " ".join(chunk)
        title = _first_sentence(chunk_text) or f"Chapter {index + 1}"
        summary = textwrap.shorten(chunk_text, width=360, placeholder="…")
        slot = "body"
        if index == 0:
            slot = "intro"
        elif index == target_chapters - 1:
            slot = "cta"
        chapters.append(
            {
                "title": title.strip()[:160],
                "summary": summary,
                "slot": slot,
                "keywords": _extract_keywords(chunk_text, limit=4),
                "chapter_index": index,
            }
        )
        start += per_chapter
        index += 1
    return chapters


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


def _format_chapters_for_prompt(chapters: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for idx, chap in enumerate(chapters, start=1):
        title = (chap or {}).get("title") or f"Section {idx}"
        summary = (chap or {}).get("summary") or ""
        lines.append(f"{idx}. {title.strip()}\n   Summary: {summary.strip()}")
    return "\n".join(lines)


def _redact_http_body_for_logging(text: Optional[str], max_length: int = 1000) -> str:
    """Return a redacted, length-bounded representation of an HTTP body for logging.

    Attempts to parse JSON and mask values for sensitive keys. Falls back to
    pattern-based redaction for bearer tokens and similar secrets. The result is
    always truncated to ``max_length`` characters and this helper must never raise.
    """
    try:
        if text is None:
            return ""

        raw = str(text)
        if not raw:
            return ""

        # First try JSON to allow structured key-based redaction.
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None

        sensitive_keys = {
            "password",
            "token",
            "access_token",
            "refresh_token",
            "authorization",
            "api_key",
            "secret",
            "client_secret",
            "email",
            "ssn",
        }

        def _redact_obj(obj: Any) -> Any:
            if isinstance(obj, dict):
                redacted: Dict[str, Any] = {}
                for k, v in obj.items():
                    key_lower = str(k).lower()
                    if key_lower in sensitive_keys:
                        redacted[k] = "[REDACTED]"
                    else:
                        redacted[k] = _redact_obj(v)
                return redacted
            if isinstance(obj, list):
                return [_redact_obj(item) for item in obj]
            # For long primitives, keep a short preview only.
            if isinstance(obj, str) and len(obj) > 256:
                return obj[:128] + "…[TRUNCATED]"
            return obj

        if parsed is not None:
            safe = json.dumps(_redact_obj(parsed), ensure_ascii=False)
        else:
            # Non-JSON body: apply regex-based redaction for common patterns.
            safe = raw
            patterns = [
                # Bearer / authorization tokens
                re.compile(r"bearer\s+[A-Za-z0-9\-_.=:+/]{10,}", re.IGNORECASE),
                re.compile(r"authorization:\s*\S+", re.IGNORECASE),
                # Generic API keys / tokens in key=value style
                re.compile(r"(api[_-]?key|token|secret)\s*[:=]\s*[A-Za-z0-9\-_.=:+/]{8,}", re.IGNORECASE),
                # Email addresses
                re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
            ]
            for pat in patterns:
                safe = pat.sub("[REDACTED]", safe)

        if len(safe) > max_length:
            return safe[: max_length - 3] + "..."
        return safe
    except Exception:
        return "<non-textual-body>"


async def compose_blog_from_text(
    text: str,
    tone: str = "informative",
    *,
    length_hint: Optional[str] = None,
    title_hint: Optional[str] = None,
    extra_context: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """One-step helper: go from raw text to a composed blog via a single AI model call.

    This reuses the existing outline + SEO helpers to provide structure, and routes
    the request through the configured AI Gateway/provider instead of making a
    direct OpenAI SDK call.
    """
    clean_text = _coerce_text(text)
    if not clean_text:
        raise ValueError("compose_blog_from_text requires non-empty text")

    outline = generate_outline(clean_text, max_sections=5)
    seo_meta = generate_seo_metadata(clean_text, outline)
    if title_hint:
        seo_meta["title"] = title_hint.strip() or seo_meta.get("title")

    length_hint = (length_hint or "standard").strip().lower()
    if length_hint not in {"short", "standard", "long"}:
        length_hint = "standard"

    # Map length_hint into textual guidance; we still rely on OPENAI_BLOG_MAX_OUTPUT_TOKENS
    # to bound tokens.
    if length_hint == "short":
        length_req = "Length: 500-800 words."
    elif length_hint == "long":
        length_req = "Length: 1300-1700 words."
    else:
        length_req = "Length: 900-1200 words."

    chapters = organize_chapters(clean_text, target_chapters=4)
    prompt_outline = _format_chapters_for_prompt(chapters) if chapters else ""
    keywords = ", ".join(seo_meta.get("keywords", []))

    system_prompt = (
        "You are Quill's senior marketing copywriter. "
        "Given source text plus lightweight outline and SEO metadata, craft a comprehensive, "
        "engaging, and factually consistent blog article. Always write in Markdown with a "
        "single H1 title followed by well-structured H2/H3 sections, short paragraphs, "
        "scannable bullet lists, and a concluding call-to-action."
    )

    requirements = [
        f"Tone: {tone}",
        f"Primary title hint: {seo_meta.get('title') or title_hint or ''}",
        f"Meta description guidance: {seo_meta.get('description') or ''}",
        f"Target keywords: {keywords or 'use best-fit based on the text'}",
        length_req,
        "Add SEO-friendly subheadings, numbered/bullet lists where useful, and contextual transitions between sections.",
        "Do not include markdown frontmatter or HTML—return pure Markdown body.",
        "Keep factual claims grounded in the provided text; do not invent statistics.",
        "Close with a concise CTA tailored to the topic.",
    ]
    merged_context = dict(extra_context or {})
    if merged_context:
        requirements.append(f"Additional context: {json.dumps(merged_context, default=str)[:800]}")

    user_prompt_parts: List[str] = [
        "Generate a publication-ready blog article from the following source text.",
        "\n\nSource text (may be truncated for length):\n",
        clean_text,
    ]
    if prompt_outline:
        user_prompt_parts.extend([
            "\n\nHeuristic outline (for guidance only):\n",
            prompt_outline,
        ])
    user_prompt_parts.extend([
        "\n\nRequirements:\n- ",
        "\n- ".join(req for req in requirements if req.strip()),
    ])
    user_prompt = "".join(user_prompt_parts)

    # Resolve model / temperature from existing blog defaults
    model_name = (model or settings.openai_blog_model or "gpt-5.1").strip()
    temp_value = temperature if temperature is not None else settings.openai_blog_temperature

    logger.info(
        "openai_compose_blog_from_text_request",
        extra={
            "model": model_name,
            "temperature": temp_value,
            "text_length": len(clean_text),
            "keywords": len(seo_meta.get("keywords", [])),
            "prompt_length": len(user_prompt),
            "openai_api_base": getattr(settings, "openai_api_base", None),
            "ai_gateway_token_set": bool(getattr(settings, "cf_ai_gateway_token", None)),
        },
    )

    # Call Cloudflare AI Gateway compat endpoint directly using AsyncSimpleClient,
    # mirroring the debug_ai_gateway_test endpoint.
    if not settings.cloudflare_account_id or not getattr(settings, "cf_ai_gateway_token", None):
        logger.error(
            "ai_gateway_missing_config",
            extra={
                "has_account_id": bool(settings.cloudflare_account_id),
                "has_token": bool(getattr(settings, "cf_ai_gateway_token", None)),
            },
        )
        raise RuntimeError("Cloudflare AI Gateway configuration is missing")

    # Use the OpenAI-compatible route configured in Cloudflare AI Gateway.
    # Example from dashboard:
    #   baseURL = "https://gateway.ai.cloudflare.com/v1/{account_id}/quill/openai"
    # and then POST /chat/completions
    gateway_base = f"https://gateway.ai.cloudflare.com/v1/{settings.cloudflare_account_id}/quill/openai"
    endpoint_path = "/chat/completions"
    client = AsyncSimpleClient(base_url=gateway_base, timeout=25.0)

    try:
        response = await client.post(
            endpoint_path,
            headers={
                "Content-Type": "application/json",
                "cf-aig-authorization": f"Bearer {settings.cf_ai_gateway_token}",
            },
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temp_value,
                # GPT-4.1/5.1 style models use max_completion_tokens instead of max_tokens.
                "max_completion_tokens": settings.openai_blog_max_output_tokens,
            },
        )
        response.raise_for_status()
    except HTTPStatusError as exc:
        logger.error(
            "gateway_compose_blog_http_error",
            exc_info=True,
            extra={
                "status_code": exc.response.status_code,
                "body": _redact_http_body_for_logging(getattr(exc.response, "text", None)),
            },
        )
        raise
    except RequestError as exc:
        logger.error("gateway_compose_blog_request_error", exc_info=True, extra={"error": str(exc)})
        raise
    except Exception:
        logger.error("gateway_compose_blog_unexpected_error", exc_info=True)
        raise

    try:
        data = response.json()
    except Exception:
        logger.error(
            "gateway_compose_blog_invalid_json",
            extra={"body_preview": _redact_http_body_for_logging(getattr(response, "text", None))},
        )
        raise

    # Extract markdown text from compat JSON response: choices[0].message.content.
    # Newer OpenAI/Gateway responses may return content as a list of segments.
    output_text: str = ""
    try:
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                output_text = content
            elif isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if isinstance(part, dict):
                        text_val = part.get("text") or part.get("content")
                        if isinstance(text_val, str):
                            parts.append(text_val)
                output_text = "".join(parts)
    except Exception:
        output_text = ""

    markdown = (output_text or "").strip()
    if not markdown:
        logger.error(
            "gateway_compose_blog_empty_content",
            extra={"body_preview": _redact_http_body_for_logging(json.dumps(data, ensure_ascii=False))},
        )

        # Best-effort fallback: if a custom model returned empty content, retry once
        # with the configured default blog model before failing. This guards against
        # occasional provider/model quirks while keeping behavior deterministic.
        default_model = (settings.openai_blog_model or "gpt-5.1").strip()
        if model_name != default_model:
            logger.info(
                "compose_blog_from_text_retry_with_default_model",
                extra={
                    "requested_model": model_name,
                    "fallback_model": default_model,
                },
            )
            # Recurse with the default model; this path will not retry again because
            # model_name will equal default_model on the second call.
            return await compose_blog_from_text(
                text,
                tone=tone,
                length_hint=length_hint,
                title_hint=title_hint,
                extra_context=extra_context,
                model=default_model,
                temperature=temperature,
            )

        raise RuntimeError("compose_blog_from_text received empty content from AI Gateway response")

    word_count = len(markdown.split())
    meta = {
        "tone": tone,
        "sections": len(chapters or []),
        "word_count": word_count,
        "engine": "openai",
        "model": model_name,
        "temperature": temp_value,
    }
    # Merge in SEO metadata fields for convenience
    meta.update({
        "title": seo_meta.get("title"),
        "description": seo_meta.get("description"),
        "keywords": seo_meta.get("keywords"),
        "slug": seo_meta.get("slug"),
    })
    return {"markdown": markdown, "meta": meta}
