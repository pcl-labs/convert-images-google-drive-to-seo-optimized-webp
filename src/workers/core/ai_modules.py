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
from .seo import resolve_schema_type

logger = logging.getLogger(__name__)


def _coerce_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return str(text).strip()


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    cleaned = _SLUG_RE.sub("-", text.lower()).strip("-")
    return cleaned or "post"


def generate_seo_metadata(
    text: str,
    *,
    content_type: Optional[str] = None,
    schema_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Return lightweight SEO metadata derived from text + schema hints."""
    base_text = _coerce_text(text)
    stored_content_type, resolved_schema, content_hint = resolve_schema_type(content_type or "generic_blog", schema_type)
    title = textwrap.shorten(base_text, width=60, placeholder="…") or "Untitled Draft"

    normalized_hint = content_hint.lower()
    if normalized_hint in {"faq", "faq_page"} and not title.lower().startswith("faq"):
        title = f"FAQ: {title}"
    elif normalized_hint in {"how_to", "howto", "how-to"} and not title.lower().startswith("how to"):
        title = f"How to {title}"
    elif normalized_hint == "recipe" and not title.lower().startswith("recipe"):
        title = f"Recipe: {title}"
    elif normalized_hint == "course" and "course" not in title.lower():
        title = f"{title} Course"

    description = textwrap.shorten(base_text, width=180, placeholder="…") if base_text else title
    words = re.findall(r"[a-z0-9]{4,}", base_text.lower())
    keywords: List[str] = []
    for word in words:
        if word not in keywords:
            keywords.append(word)
        if len(keywords) >= 12:
            break
    return {
        "title": title,
        "description": description,
        "slug": _slugify(title),
        "keywords": keywords,
        "hero_image": None,
        "content_type": stored_content_type,
        "schema_type": resolved_schema,
        "content_hint": content_hint,
    }


def generate_image_prompts(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Produce simple illustrative prompts per section for downstream image generation."""
    prompts: List[Dict[str, Any]] = []
    for idx, section in enumerate(sections):
        title = (section or {}).get("title") or f"Section {idx + 1}"
        summary = (section or {}).get("summary") or ""
        prompts.append(
            {
                "section_index": idx,
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

    Routes the request through the configured AI Gateway/provider instead of making a
    direct OpenAI SDK call.
    """
    clean_text = _coerce_text(text)
    if not clean_text:
        raise ValueError("compose_blog_from_text requires non-empty text")

    extra_meta_context = extra_context or {}
    seo_meta = generate_seo_metadata(
        clean_text,
        content_type=extra_meta_context.get("content_type"),
        schema_type=extra_meta_context.get("schema_type"),
    )
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

    keywords = ", ".join(seo_meta.get("keywords", []))

    system_prompt = (
        "You are Quill's senior marketing copywriter. "
        "Given source text and SEO metadata, craft a comprehensive, "
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
        "\n\nRequirements:\n- ",
        "\n- ".join(req for req in requirements if req.strip()),
    ]
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
        "sections": 0,  # Sections are extracted later from MDX content
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
        "schema_type": seo_meta.get("schema_type"),
        "content_type": seo_meta.get("content_type"),
        "content_hint": seo_meta.get("content_hint"),
    })
    return {"markdown": markdown, "meta": meta}
