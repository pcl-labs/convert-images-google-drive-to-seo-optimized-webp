import asyncio

import pytest

from src.workers.core import content_planner


@pytest.mark.asyncio
async def test_plan_content_uses_fallback_when_openai_unavailable(monkeypatch):
    async def _raise(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(content_planner, "_plan_with_openai", _raise)
    plan = await content_planner.plan_content(
        "Short transcript about building communities online.",
        content_type="generic_blog",
        max_sections=3,
        target_chapters=2,
    )
    assert plan["provider"] == "fallback"
    assert plan["sections"]
    assert plan["outline"][0]["slot"] == "intro"
    assert plan["planner_attempts"] == 2
    assert plan["planner_error"] == "boom"


@pytest.mark.asyncio
async def test_plan_content_merges_openai_payload(monkeypatch):
    async def _mock_plan(**kwargs):
        return {
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

    monkeypatch.setattr(content_planner, "_plan_with_openai", _mock_plan)
    plan = await content_planner.plan_content("Transcript body", content_type="generic_blog")
    assert plan["provider"] == "openai"
    assert plan["sections"][0]["title"] == "Hook readers with a mission"
    assert plan["sections"][1]["cta"] is True
    assert plan["outline"][0]["slot"] == "intro"
    assert plan["outline"][-1]["slot"] == "cta"
    assert plan["seo"]["title"] == "Custom Title"
    assert plan["planner_model"] == "gpt-5.1"
