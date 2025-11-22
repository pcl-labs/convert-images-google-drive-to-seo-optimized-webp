import asyncio

import pytest

from src.workers.core import content_planner


@pytest.mark.asyncio
async def test_plan_content_fallback_only():
    plan = await content_planner.plan_content(
        "Short transcript about building communities online.",
        content_type="generic_blog",
        max_sections=3,
        target_chapters=2,
    )
    assert plan["provider"] == "fallback"
    assert plan["sections"]
    assert plan["outline"][0]["slot"] == "intro"
    assert plan["planner_attempts"] == 0
    assert plan["planner_error"] is None


def test_merge_plans_prefers_ai_sections_and_seo():
    ai_payload = {
        "intent": "persuade",
        "audience": "founders",
        "seo": {"title": "Custom Title", "description": "Custom Desc", "keywords": ["alpha", "beta"]},
        "sections": [
            {
                "title": "Hook readers with a mission",
                "summary": "Explain why the nonprofit exists and the impact so far.",
                "purpose": "intro",
                "key_points": ["Mission statement", "Region served"],
                "cta": False,
            },
            {
                "title": "CTA",
                "summary": "Encourage the reader to book a consult.",
                "purpose": "cta",
                "key_points": ["Sliding scale fees"],
                "cta": True,
                "call_to_action": "Book a consultation",
            },
        ],
        "cta": {"summary": "Ready to help?", "action": "Schedule a consult"},
    }

    fallback = content_planner._fallback_plan(
        text="Transcript body",
        content_type="generic_blog",
        max_sections=5,
        target_chapters=4,
        instructions=None,
    )

    plan = content_planner._merge_plans("generic_blog", fallback, ai_payload, instructions=None)
    assert plan["provider"] == "openai"
    assert plan["sections"][0]["title"] == "Hook readers with a mission"
    assert plan["sections"][1]["cta"] is True
    assert plan["outline"][0]["slot"] == "intro"
    assert plan["outline"][-1]["slot"] == "cta"
    assert plan["seo"]["title"] == "Custom Title"
