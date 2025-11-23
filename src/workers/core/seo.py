from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SCHEMA_TYPE = "https://schema.org/BlogPosting"

SCHEMA_TYPE_BY_HINT: Dict[str, str] = {
    "generic_blog": "https://schema.org/BlogPosting",
    "blog": "https://schema.org/BlogPosting",
    "blog_post": "https://schema.org/BlogPosting",
    "blogposting": "https://schema.org/BlogPosting",
    "faq": "https://schema.org/FAQPage",
    "faq_page": "https://schema.org/FAQPage",
    "how_to": "https://schema.org/HowTo",
    "howto": "https://schema.org/HowTo",
    "recipe": "https://schema.org/Recipe",
    "course": "https://schema.org/Course",
}

HINT_ALIASES: Dict[str, str] = {
    "blogposting": "generic_blog",
    "blog_post": "generic_blog",
    "blog-post": "generic_blog",
    "post": "generic_blog",
    "faqpage": "faq",
    "faq-page": "faq",
    "howto": "how_to",
    "how-to": "how_to",
}

HINT_BY_SCHEMA: Dict[str, str] = {schema: hint for hint, schema in SCHEMA_TYPE_BY_HINT.items()}


def normalize_content_type_value(value: Optional[str]) -> str:
    """Return the stored content_type string with a sensible fallback."""
    if not value:
        return "generic_blog"
    cleaned = str(value).strip()
    return cleaned or "generic_blog"


def derive_content_type_hint(content_type: str, schema_type: Optional[str] = None) -> str:
    """Map user-provided content_type/schema_type to a canonical hint string."""
    lowered = content_type.strip().lower()
    if lowered in HINT_ALIASES:
        lowered = HINT_ALIASES[lowered]
    if lowered in SCHEMA_TYPE_BY_HINT:
        return lowered
    if schema_type:
        hint = HINT_BY_SCHEMA.get(schema_type.strip())
        if hint:
            return hint
    if lowered.startswith("http"):
        return HINT_BY_SCHEMA.get(lowered, "custom")
    return lowered or "generic_blog"


def resolve_schema_type(content_type: str, schema_type: Optional[str]) -> Tuple[str, str, str]:
    """
    Return a tuple of (content_type_value, schema_type_value, content_type_hint).

    - Preserves the provided ``content_type`` string for backwards-compatibility.
    - Derives ``schema_type`` from hints or schema URLs when missing.
    - Always emits a canonical ``content_type_hint`` for downstream logic.
    """
    stored_content_type = normalize_content_type_value(content_type)
    normalized_schema = (schema_type or "").strip()

    if normalized_schema:
        hint = derive_content_type_hint(stored_content_type, normalized_schema)
        # When content_type is empty but schema_type points to a known type,
        # expose the canonical hint for downstream usage.
        if hint == "custom":
            hint = HINT_BY_SCHEMA.get(normalized_schema, stored_content_type.lower())
        return stored_content_type, normalized_schema, hint or "generic_blog"

    if stored_content_type.startswith("http"):
        normalized_schema = stored_content_type
    else:
        hint_candidate = derive_content_type_hint(stored_content_type)
        normalized_schema = SCHEMA_TYPE_BY_HINT.get(hint_candidate, DEFAULT_SCHEMA_TYPE)

    hint_value = derive_content_type_hint(stored_content_type, normalized_schema)
    if hint_value == "custom" and stored_content_type.startswith("http"):
        hint_value = HINT_BY_SCHEMA.get(stored_content_type, "custom")
    return stored_content_type, normalized_schema or DEFAULT_SCHEMA_TYPE, hint_value or "generic_blog"


def build_structured_content(content_hint: str, sections: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Derive structured data payloads (FAQ items, how-to steps, lessons) from sections."""
    normalized_hint = (content_hint or "generic_blog").lower()
    normalized_sections: List[Dict[str, Any]] = []
    for idx, raw in enumerate(sections or []):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        summary = str(raw.get("summary") or raw.get("body_mdx") or "").strip()
        if not title and not summary:
            continue
        normalized_sections.append(
            {
                "title": title or f"Section {idx + 1}",
                "summary": summary,
                "order": int(raw.get("order", idx)),
                "section_id": raw.get("section_id") or f"sec-{idx}",
            }
        )

    if not normalized_sections:
        return None

    if normalized_hint in {"faq", "faq_page"}:
        items = []
        for item in normalized_sections:
            question = item["title"]
            answer = item["summary"] or question
            items.append(
                {
                    "question": question,
                    "answer": answer,
                    "section_id": item["section_id"],
                    "order": item["order"],
                }
            )
        return {"type": "faq", "items": items}

    if normalized_hint in {"how_to", "howto", "how-to"}:
        steps = []
        for idx, item in enumerate(normalized_sections, start=1):
            steps.append(
                {
                    "title": item["title"],
                    "description": item["summary"] or item["title"],
                    "order": item["order"],
                    "position": idx,
                    "section_id": item["section_id"],
                }
            )
        return {"type": "how_to", "steps": steps}

    if normalized_hint == "course":
        lessons = []
        for idx, item in enumerate(normalized_sections, start=1):
            lessons.append(
                {
                    "title": item["title"],
                    "description": item["summary"],
                    "module_index": idx,
                    "section_id": item["section_id"],
                }
            )
        return {"type": "course", "lessons": lessons}

    if normalized_hint == "recipe":
        steps = []
        for idx, item in enumerate(normalized_sections, start=1):
            steps.append(
                {
                    "title": item["title"],
                    "instruction": item["summary"],
                    "position": idx,
                    "section_id": item["section_id"],
                }
            )
        return {"type": "recipe", "steps": steps}

    return None


def _ld_type(schema_type: str) -> str:
    if not schema_type:
        return "Thing"
    if "/" in schema_type:
        return schema_type.rsplit("/", 1)[-1]
    return schema_type


def build_schema_json_ld(
    schema_type: str,
    frontmatter: Dict[str, Any],
    structured_content: Optional[Dict[str, Any]],
    sections: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Generate a JSON-LD representation tailored to the schema/content type."""
    if not schema_type:
        return None

    fm = frontmatter or {}
    title = (fm.get("title") or "").strip()
    description = (fm.get("description") or "").strip()
    keywords = fm.get("tags") or fm.get("keywords") or []
    image = fm.get("hero_image")
    ld_type = _ld_type(schema_type)

    base: Dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": ld_type,
    }
    if title:
        base["headline"] = title
        base["name"] = title
    if description:
        base["description"] = description
    if keywords:
        base["keywords"] = keywords
    if image:
        base["image"] = image

    if ld_type == "FAQPage":
        items = (structured_content or {}).get("items") if structured_content else None
        if not items and sections:
            structured = build_structured_content("faq", sections)
            items = (structured or {}).get("items")
        if not items:
            return base
        base["mainEntity"] = [
            {
                "@type": "Question",
                "name": item.get("question"),
                "acceptedAnswer": {"@type": "Answer", "text": item.get("answer")},
            }
            for item in items
            if item.get("question") and item.get("answer")
        ]
        return base

    if ld_type == "HowTo":
        steps = (structured_content or {}).get("steps") if structured_content else None
        if not steps and sections:
            structured = build_structured_content("how_to", sections)
            steps = (structured or {}).get("steps")
        if not steps:
            return base
        base["step"] = [
            {
                "@type": "HowToStep",
                "name": step.get("title"),
                "text": step.get("description"),
                "position": step.get("position"),
            }
            for step in steps
            if step.get("title")
        ]
        return base

    if ld_type == "Course":
        lessons = (structured_content or {}).get("lessons") if structured_content else None
        if not lessons and sections:
            structured = build_structured_content("course", sections)
            lessons = (structured or {}).get("lessons")
        if not lessons:
            return base
        base["hasCourseInstance"] = [
            {
                "@type": "CourseInstance",
                "name": lesson.get("title"),
                "description": lesson.get("description"),
                "courseMode": "online",
            }
            for lesson in lessons
            if lesson.get("title")
        ]
        return base

    if ld_type == "Recipe":
        steps = (structured_content or {}).get("steps") if structured_content else None
        if not steps and sections:
            structured = build_structured_content("recipe", sections)
            steps = (structured or {}).get("steps")
        if steps:
            base["recipeInstructions"] = [
                {
                    "@type": "HowToStep",
                    "text": step.get("instruction") or step.get("title"),
                    "position": step.get("position"),
                }
                for step in steps
                if step.get("instruction") or step.get("title")
            ]
        return base

    # Default BlogPosting article metadata
    if ld_type == "BlogPosting":
        if sections:
            base["articleSection"] = [sec.get("title") for sec in sections if sec.get("title")]
    return base
