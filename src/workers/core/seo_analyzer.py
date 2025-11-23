from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import math
import re

from .seo import (
    resolve_schema_type,
    build_structured_content,
    build_schema_json_ld,
)
from .schema_validator import validate_schema_json_ld


def _strip_markdown(text: str) -> str:
    """Convert Markdown/MDX into lightweight plaintext for scoring."""
    if not text:
        return ""
    clean = re.sub(r"`{1,3}[^`]+`{1,3}", " ", text)
    clean = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", clean)
    clean = re.sub(r"^#{1,6}\s*", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"[*>_-]{2,}", " ", clean)
    return clean


def _estimate_syllables(word: str) -> int:
    word = word.lower()
    word = re.sub(r"[^a-z]", "", word)
    if not word:
        return 1
    vowels = "aeiouy"
    count = 0
    prev_was_vowel = False
    for char in word:
        is_vowel = char in vowels
        if is_vowel and not prev_was_vowel:
            count += 1
        prev_was_vowel = is_vowel
    if word.endswith("e") and count > 1:
        count -= 1
    return max(1, count)


def _flesch_score(text: str) -> float:
    words = re.findall(r"[A-Za-z0-9']+", text)
    if not words:
        return 100.0
    sentences = max(1, len(re.findall(r"[.!?]", text)))
    syllables = sum(_estimate_syllables(word) for word in words)
    words_count = len(words)
    score = 206.835 - 1.015 * (words_count / max(1, sentences)) - 84.6 * (syllables / words_count)
    return max(0.0, min(100.0, score))


def _heading_score(body_mdx: str, sections: List[Dict[str, Any]]) -> float:
    headings = re.findall(r"^#{2,4}\s+", body_mdx or "", flags=re.MULTILINE)
    desired = max(3, len(sections))
    if desired <= 0:
        desired = 3
    ratio = len(headings) / desired
    return max(0.0, min(100.0, ratio * 100))


def _meta_score(title: str, description: str) -> float:
    title_len = len(title or "")
    desc_len = len(description or "")
    title_ok = 50 <= title_len <= 65
    desc_ok = 120 <= desc_len <= 180
    if title_ok and desc_ok:
        return 95.0
    if title_ok or desc_ok:
        return 70.0
    return 40.0


def _keyword_score(text: str, keywords: List[str]) -> Tuple[float, Dict[str, int]]:
    if not keywords:
        return 100.0, {}
    counts: Dict[str, int] = {}
    for keyword in keywords:
        if not keyword:
            continue
        # Use word-boundary matching so "cat" does not match "category".
        pattern = re.compile(r"\\b" + re.escape(str(keyword).strip()) + r"\\b", flags=re.IGNORECASE)
        occurrences = len(pattern.findall(text))
        counts[keyword] = occurrences
    target_hits = sum(1 for count in counts.values() if count >= 2)
    coverage = target_hits / max(1, len(keywords))
    score = min(100.0, coverage * 100)
    return score, counts


def _schema_score(schema_json: Optional[Dict[str, Any]], content_hint: str) -> float:
    if schema_json:
        return 95.0
    if content_hint in {"faq", "how_to", "course", "recipe"}:
        return 45.0
    return 70.0


def _level_from_score(score: float) -> str:
    if score >= 80:
        return "good"
    if score >= 55:
        return "average"
    return "poor"


def _normalize_keywords(*candidates: Any) -> List[str]:
    seen: List[str] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            values = [candidate]
        elif isinstance(candidate, list):
            values = candidate
        else:
            continue
        for val in values:
            term = str(val or "").strip()
            if not term:
                continue
            if term.lower() not in (existing.lower() for existing in seen):
                seen.append(term)
    return seen[:20]


def analyze_seo_document(
    *,
    frontmatter: Optional[Dict[str, Any]],
    body_mdx: Optional[str],
    sections: Optional[List[Dict[str, Any]]],
    content_plan: Optional[Dict[str, Any]],
    assets: Optional[Dict[str, Any]],
    raw_content_type: Optional[str],
    schema_type: Optional[str],
    target_keywords: Optional[List[str]] = None,
    focus_keyword: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze SEO for a blog version and emit Yoast-style scores + suggestions."""
    frontmatter = frontmatter or {}
    body_mdx = body_mdx or ""
    sections = sections or []
    content_plan = content_plan or {}
    assets = assets or {}

    stored_content_type, resolved_schema, content_hint = resolve_schema_type(raw_content_type or "generic_blog", schema_type)

    plan_seo = content_plan.get("seo") if isinstance(content_plan, dict) else {}
    if not isinstance(plan_seo, dict):
        plan_seo = {}
    seo_payload = {
        "title": frontmatter.get("title") or plan_seo.get("title"),
        "description": frontmatter.get("description") or plan_seo.get("description"),
        "slug": frontmatter.get("slug") or plan_seo.get("slug"),
        "keywords": plan_seo.get("keywords") or frontmatter.get("tags") or [],
        "hero_image": frontmatter.get("hero_image") or plan_seo.get("hero_image"),
        "schema_type": resolved_schema,
        "content_type": stored_content_type,
        "content_hint": content_hint,
    }
    if plan_seo.get("json_ld"):
        seo_payload["json_ld"] = plan_seo["json_ld"]

    schema_asset = (assets.get("schema") or {}) if isinstance(assets, dict) else {}
    if schema_asset.get("json_ld") and not seo_payload.get("json_ld"):
        seo_payload["json_ld"] = schema_asset.get("json_ld")

    structured_content = content_plan.get("structured") if isinstance(content_plan, dict) else None
    if not structured_content and isinstance(assets, dict):
        structured_content = assets.get("structured_content")
    if not structured_content and content_hint in {"faq", "how_to", "course", "recipe"}:
        structured_content = build_structured_content(content_hint, sections)

    if not seo_payload.get("json_ld"):
        schema_json = build_schema_json_ld(resolved_schema, frontmatter, structured_content, sections)
        if schema_json:
            seo_payload["json_ld"] = schema_json

    schema_validation: Optional[Dict[str, Any]] = None
    if seo_payload.get("json_ld"):
        schema_validation = validate_schema_json_ld(
            seo_payload["json_ld"],
            schema_type=resolved_schema,
            content_hint=content_hint,
        )

    keywords = _normalize_keywords(target_keywords, seo_payload.get("keywords"), frontmatter.get("tags"))
    if focus_keyword:
        keywords = [focus_keyword] + [kw for kw in keywords if kw.lower() != focus_keyword.lower()]
    seo_payload["keywords"] = keywords

    body_text = _strip_markdown(body_mdx)
    word_count = len(body_text.split())
    reading_time_seconds = math.ceil(word_count / 180 * 60) if word_count else 0

    readability_value = _flesch_score(body_text)
    keyword_value, keyword_counts = _keyword_score(body_text, keywords)
    heading_value = _heading_score(body_mdx, sections)
    meta_value = _meta_score(seo_payload.get("title") or "", seo_payload.get("description") or "")
    schema_value = _schema_score(seo_payload.get("json_ld"), content_hint)

    scores: List[Dict[str, Any]] = [
        {
            "name": "readability",
            "label": "Readability",
            "score": round(readability_value, 2),
            "level": _level_from_score(readability_value),
            "details": "Flesch reading ease target 60-80",
        },
        {
            "name": "keywords",
            "label": "Keyword focus",
            "score": round(keyword_value, 2),
            "level": _level_from_score(keyword_value),
            "details": f"{sum(keyword_counts.values())} total keyword mentions",
        },
        {
            "name": "headings",
            "label": "Heading structure",
            "score": round(heading_value, 2),
            "level": _level_from_score(heading_value),
            "details": f"{len(re.findall(r'^#{2,4}\\s+', body_mdx, flags=re.MULTILINE))} H2/H3 headings detected",
        },
        {
            "name": "metadata",
            "label": "Meta tags",
            "score": round(meta_value, 2),
            "level": _level_from_score(meta_value),
            "details": "Optimizes title (50-65 chars) and description (120-180 chars)",
        },
        {
            "name": "schema",
            "label": "Structured data",
            "score": round(schema_value, 2),
            "level": _level_from_score(schema_value),
            "details": "JSON-LD presence for schema-aware content",
        },
    ]

    suggestions: List[Dict[str, Any]] = []

    def _add_suggestion(metric: str, title: str, summary: str, severity: str = "warning"):
        suggestions.append(
            {
                "id": f"{metric}-{len(suggestions)+1}",
                "title": title,
                "summary": summary,
                "severity": severity,
                "metric": metric,
            }
        )

    if scores[0]["level"] == "poor":
        _add_suggestion(
            "readability",
            "Improve readability",
            "Shorten sentences, break up dense paragraphs, and mix in bullet lists for easier scanning.",
        )
    if scores[1]["level"] == "poor" and keywords:
        _add_suggestion(
            "keywords",
            "Increase keyword coverage",
            "Mention each target keyword at least twice—especially in the introduction and subheadings.",
        )
    if scores[2]["level"] == "poor":
        _add_suggestion(
            "headings",
            "Add descriptive headings",
            "Introduce more H2/H3 headings so every major idea has a scannable subheading.",
        )
    if scores[3]["level"] == "poor":
        _add_suggestion(
            "metadata",
            "Tighten title & description",
            "Keep titles between 50-65 characters and descriptions near 150 characters for best SERP display.",
        )
    if scores[4]["level"] == "poor":
        _add_suggestion(
            "schema",
            "Add structured data",
            "Attach a JSON-LD block for the selected schema so search engines can render rich snippets.",
        )
    if not frontmatter.get("hero_image"):
        _add_suggestion(
            "media",
            "Add a hero image",
            "Set a featured image for improved click-throughs on social cards and SERPs.",
            severity="info",
        )
    if content_hint in {"faq", "how_to", "course", "recipe"} and not structured_content:
        _add_suggestion(
            "structured",
            "Outline structured blocks",
            "Add explicit FAQ entries, course lessons, or how-to steps so schema output has concrete data.",
        )
    if focus_keyword:
        intro_segment = body_text[:400].lower()
        if focus_keyword.lower() not in intro_segment:
            _add_suggestion(
                "focus_keyword",
                "Surface the focus keyword sooner",
                f"Include “{focus_keyword}” in the opening paragraph to reinforce topical relevance.",
            )

    return {
        "seo": seo_payload,
        "scores": scores,
        "suggestions": suggestions,
        "structured_content": structured_content,
        "content_type": stored_content_type,
        "content_type_hint": content_hint,
        "schema_type": resolved_schema,
        "word_count": word_count,
        "reading_time_seconds": reading_time_seconds,
        "schema_validation": schema_validation,
    }
