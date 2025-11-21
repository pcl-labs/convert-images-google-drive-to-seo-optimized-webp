from __future__ import annotations

from typing import Any, Dict

from api.config import settings

try:
    from openai import AsyncOpenAI  # type: ignore
except Exception:  # pragma: no cover - optional dependency during tests
    AsyncOpenAI = None  # type: ignore


def get_async_openai_client(purpose: str = "default") -> AsyncOpenAI:
    """Create an AsyncOpenAI client configured to use Cloudflare AI Gateway.

    This helper centralizes how we talk to LLMs so both the content planner and
    blog composer share the same configuration and headers.

    Requirements (in Workers runtime):
    - settings.openai_api_key: upstream provider key (e.g., OpenAI)
    - settings.openai_api_base: Cloudflare AI Gateway compat base URL
    - settings.cf_ai_gateway_token: AI Gateway token for cf-aig-authorization
    """

    if AsyncOpenAI is None:
        raise RuntimeError("openai package is not installed")

    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for AI operations")

    client_kwargs: Dict[str, Any] = {"api_key": settings.openai_api_key}

    api_base = getattr(settings, "openai_api_base", None)
    if api_base:
        client_kwargs["base_url"] = api_base

    # When routing via Cloudflare AI Gateway, attach the gateway token header
    if api_base and "gateway.ai.cloudflare.com" in api_base:
        gateway_token = getattr(settings, "cf_ai_gateway_token", None)
        if not gateway_token:
            raise ValueError("CF_AI_GATEWAY_TOKEN is required when using AI Gateway")
        client_kwargs["default_headers"] = {
            "cf-aig-authorization": f"Bearer {gateway_token}",
        }

    return AsyncOpenAI(**client_kwargs)
