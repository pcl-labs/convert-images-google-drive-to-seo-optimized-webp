from __future__ import annotations

import json
import logging
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from openai import AsyncOpenAI, OpenAIError  # type: ignore
except Exception:  # pragma: no cover - optional dependency during tests
    AsyncOpenAI = None  # type: ignore
    OpenAIError = Exception  # type: ignore

from src.workers.api.config import settings
from .ai_modules import generate_outline, organize_chapters, generate_seo_metadata

logger = logging.getLogger(__name__)

DEFAULT_CONTENT_TYPE = "https://schema.org/BlogPosting"


@dataclass
class PlannedSection:
    order: int
    title: str
    summary: str
    purpose: str = "body"
    key_points: List[str] = field(default_factory=list)
    cta: bool = False
    call_to_action: Optional[str] = None

    def to_outline_item(self) -> Dict[str, Any]:
        slot = "intro" if self.order == 0 else ("cta" if self.cta else "body")
        return {
            "title": self.title,
            "summary": self.summary,
            "slot": slot,
            "keywords": self.key_points[:8],
            "purpose": self.purpose,
            "cta": self.cta,
        }

    def to_chapter(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "key_points": self.key_points[:8],
            "purpose": self.purpose,
            "cta": self.cta,
        }

    def to_section_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "slug": _slugify(self.title) or f"section-{self.order}",
            "title": self.title,
            "summary": self.summary,
            "purpose": self.purpose,
            "key_points": self.key_points[:8],
            "cta": self.cta,
            "call_to_action": self.call_to_action,
        }


async def plan_content(
    text: str,
    *,
    content_type: str = DEFAULT_CONTENT_TYPE,
    max_sections: int = 5,
    target_chapters: int = 4,
    instructions: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a schema-first plan for downstream composition.
    Falls back to heuristics when the OpenAI planner is unavailable.
    """
    normalized_type = (content_type or DEFAULT_CONTENT_TYPE).strip() or DEFAULT_CONTENT_TYPE
    fallback_plan = _fallback_plan(
        text=text,
        content_type=normalized_type,
        max_sections=max_sections,
        target_chapters=target_chapters,
        instructions=instructions,
    )
    planner_model = getattr(settings, "openai_planner_model", None) or settings.openai_blog_model or "gpt-5.1-mini"
    planner_attempts = 0
    planner_error: Optional[str] = None
    openai_payload: Dict[str, Any] = {}
    while planner_attempts < 2:
        planner_attempts += 1
        try:
            openai_payload = await _plan_with_openai(
                text=text,
                content_type=normalized_type,
                max_sections=max_sections,
                instructions=instructions,
                model_override=planner_model,
            )
            planner_error = None
            break
        except Exception as exc:
            planner_error = str(exc)
            logger.warning(
                "content_plan_openai_unavailable",
                exc_info=True,
                extra={"attempt": planner_attempts, "model": planner_model},
            )
            if planner_attempts >= 2:
                openai_payload = {}
                break

    merged_plan = _merge_plans(
        normalized_type,
        fallback_plan,
        openai_payload,
        instructions=instructions,
    )
    merged_plan["planner_model"] = planner_model
    merged_plan["planner_attempts"] = planner_attempts
    merged_plan["planner_error"] = planner_error
    return merged_plan


async def _plan_with_openai(
    *,
    text: str,
    content_type: str,
    max_sections: int,
    instructions: Optional[str],
    model_override: Optional[str] = None,
    temperature_override: Optional[float] = None,
    max_tokens_override: Optional[int] = None,
) -> Dict[str, Any]:
    if not settings.openai_api_key or AsyncOpenAI is None:
        raise RuntimeError("OPENAI_API_KEY missing or openai package unavailable")

    planner_model = model_override or getattr(settings, "openai_planner_model", None) or settings.openai_blog_model or "gpt-5.1-mini"
    temperature = temperature_override if temperature_override is not None else getattr(settings, "openai_planner_temperature", 0.2)
    max_tokens = max_tokens_override if max_tokens_override is not None else getattr(settings, "openai_planner_max_output_tokens", 1200)
    trimmed_text = text.strip()
    if len(trimmed_text) > 12000:
        trimmed_text = trimmed_text[:12000]

    schema_description = {
        "content_type": content_type,
        "intent": "educate",
        "audience": "general readers",
        "seo": {
            "title": "",
            "description": "",
            "keywords": [],
        },
        "sections": [],
        "cta": {
            "summary": "",
            "action": "",
        },
    }
    user_prompt = textwrap.dedent(
        f"""
        You are Quill's planning system. Given a transcript, create a content plan for {content_type}.
        Return JSON matching this schema:
        {{
          "intent": "why the article exists",
          "audience": "who it targets",
          "seo": {{"title":"", "description":"", "keywords": []}},
          "sections": [
            {{
              "title": "human-readable heading",
              "summary": "short paragraph summary",
              "purpose": "intro|body|proof|cta|tips (pick the closest)",
              "key_points": ["bullet one", "bullet two"],
              "cta": false,
              "call_to_action": "optional CTA copy"
            }}
          ],
          "cta": {{"summary": "CTA wrap-up sentence", "action": "suggested action"}}
        }}

        Transcript:
        {trimmed_text}

        Additional guidance: {instructions or "Use the transcript context to plan the best structure."}
        """
    ).strip()

    client_kwargs: Dict[str, Any] = {"api_key": settings.openai_api_key}
    if settings.openai_api_base:
        client_kwargs["base_url"] = settings.openai_api_base
    client = AsyncOpenAI(**client_kwargs)
    async with client:
        try:
            response = await client.chat.completions.create(
                model=planner_model,
                temperature=temperature,
                max_completion_tokens=max_tokens,
                messages=[
                    {
                        "role": "system",
                        "content": "Respond with valid JSON only. No prose. Plan the content structure.",
                    },
                    {"role": "user", "content": user_prompt},
                ],
            )
        except OpenAIError as exc:
            logger.warning(
                "content_plan_openai_error",
                exc_info=True,
                extra={"model": planner_model, "reason": getattr(exc, "message", str(exc))},
            )
            raise

    content = ""
    if response.choices:
        message = response.choices[0].message
        if message and message.content:
            if isinstance(message.content, str):
                content = message.content
            elif isinstance(message.content, list):
                content = "".join(part.get("text", "") for part in message.content if isinstance(part, dict))
    content = content.strip()
    if not content:
        raise RuntimeError("Planner returned empty content")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("content_plan_openai_invalid_json", extra={"content_preview": content[:200]})
        raise RuntimeError("Planner returned invalid JSON") from exc
    return payload or {}


def _merge_plans(
    content_type: str,
    fallback_plan: Dict[str, Any],
    ai_payload: Dict[str, Any],
    *,
    instructions: Optional[str],
) -> Dict[str, Any]:
    sections = _normalize_sections(ai_payload.get("sections") or [])
    provider = "openai" if sections else "fallback"
    if not sections:
        sections = _normalize_sections(fallback_plan.get("sections") or [])
    outline = [section.to_outline_item() for section in sections]
    chapters = [section.to_chapter() for section in sections]
    seo_from_ai = ai_payload.get("seo") or {}
    seo = _merge_seo(fallback_plan.get("seo") or {}, seo_from_ai)
    plan = {
        "schema": "blog.post",
        "schema_version": 1,
        "content_type": content_type,
        "intent": ai_payload.get("intent") or fallback_plan.get("intent") or "educate",
        "audience": ai_payload.get("audience") or fallback_plan.get("audience") or "general",
        "sections": [section.to_section_dict() for section in sections],
        "outline": outline or fallback_plan.get("outline"),
        "chapters": chapters or fallback_plan.get("chapters"),
        "seo": seo,
        "instructions": (instructions or "").strip() or None,
        "provider": provider,
        "cta": ai_payload.get("cta") or fallback_plan.get("cta"),
    }
    return plan


def _fallback_plan(
    *,
    text: str,
    content_type: str,
    max_sections: int,
    target_chapters: int,
    instructions: Optional[str],
) -> Dict[str, Any]:
    outline = generate_outline(text, max_sections)
    chapters = organize_chapters(text, target_chapters)
    if not chapters and outline:
        chapters = [
            {"title": item.get("title"), "summary": item.get("summary")}
            for item in outline
            if item
        ]
    if not chapters:
        summary = textwrap.shorten(text or "", width=360, placeholder="…")
        chapters = [{"title": "Overview", "summary": summary}]
    sections = [
        PlannedSection(
            order=idx,
            title=(chapter.get("title") or f"Section {idx + 1}").strip(),
            summary=chapter.get("summary") or "",
            purpose="intro" if idx == 0 else ("cta" if idx == len(chapters) - 1 else "body"),
            key_points=_coerce_key_points(chapter.get("key_points")),
            cta=idx == len(chapters) - 1,
        )
        for idx, chapter in enumerate(chapters)
    ]
    return {
        "content_type": content_type,
        "outline": outline,
        "chapters": chapters,
        "sections": [section.to_section_dict() for section in sections],
        "seo": generate_seo_metadata(text, outline),
        "intent": "educate",
        "audience": "general",
        "instructions": (instructions or "").strip() or None,
        "provider": "fallback",
        "cta": {"summary": "Summarize the key takeaways and invite the reader to act."},
    }


def _normalize_sections(raw_sections: List[Dict[str, Any]]) -> List[PlannedSection]:
    normalized: List[PlannedSection] = []
    for idx, section in enumerate(raw_sections):
        if not isinstance(section, dict):
            continue
        title = (section.get("title") or f"Section {idx + 1}").strip()
        summary = (section.get("summary") or "").strip()
        if not title or not summary:
            continue
        purpose = (section.get("purpose") or ("intro" if idx == 0 else "body")).strip()
        if purpose not in {"intro", "body", "proof", "cta", "tips"}:
            purpose = "body"
        key_points = _coerce_key_points(section.get("key_points"))
        normalized.append(
            PlannedSection(
                order=idx,
                title=title,
                summary=summary,
                purpose=purpose,
                key_points=key_points,
                cta=bool(section.get("cta")) or purpose == "cta",
                call_to_action=(section.get("call_to_action") or "").strip() or None,
            )
        )
    return normalized


def _merge_seo(
    fallback_seo: Dict[str, Any],
    ai_seo: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(ai_seo, dict):
        return fallback_seo
    merged = dict(fallback_seo or {})
    for key in ("title", "description", "slug"):
        if ai_seo.get(key):
            merged[key] = ai_seo[key]
    if ai_seo.get("keywords") and isinstance(ai_seo["keywords"], list):
        merged["keywords"] = ai_seo["keywords"][:10]
    if ai_seo.get("hero_image"):
        merged["hero_image"] = ai_seo["hero_image"]
    return merged


def _coerce_key_points(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split("•") if part.strip()]
    return []


def _slugify(text: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in text.strip())
    slug = "-".join(filter(None, slug.split("-")))
    return slug[:80]
