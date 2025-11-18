from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import settings
from .models import GenerateBlogOptions

DEFAULT_MODEL_CHOICES: List[Dict[str, str]] = [
    {"value": "gpt-4o-mini", "label": "GPT-4o mini"},
    {"value": "gpt-4.1-mini", "label": "GPT-4.1 mini"},
    {"value": "gpt-4.1", "label": "GPT-4.1"},
    {"value": "gpt-5.1-mini", "label": "GPT-5.1 mini"},
    {"value": "gpt-5.1", "label": "GPT-5.1"},
]


def _default_model() -> str:
    # Use the configured blog model or fall back to GPT-5.1, our current target model.
    # See https://platform.openai.com/docs/models/gpt-5.1
    return settings.openai_blog_model or "gpt-5.1"


def get_ai_model_choices() -> List[Dict[str, str]]:
    """Return available OpenAI model choices, ensuring configured model appears."""
    configured = _default_model()
    seen = {entry["value"] for entry in DEFAULT_MODEL_CHOICES}
    choices = list(DEFAULT_MODEL_CHOICES)
    if configured not in seen:
        choices.insert(0, {"value": configured, "label": configured})
    return choices


def _boolish(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return fallback


def _clamp_temperature(value: Optional[float]) -> float:
    base = settings.openai_blog_temperature or 0.6
    temp = value if isinstance(value, (float, int)) else base
    return max(0.0, min(float(temp), 2.0))


def default_ai_preferences() -> Dict[str, Any]:
    """Return baseline AI preference values pulled from settings."""
    return {
        "provider": "openai",
        "model": _default_model(),
        "tone": "informative",
        "temperature": settings.openai_blog_temperature or 0.6,
        "max_sections": 5,
        "target_chapters": 4,
        "include_images": True,
        "content_type": "generic_blog",
    }


def normalize_ai_preferences(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge stored preference blob with defaults."""
    defaults = default_ai_preferences()
    if not isinstance(raw, dict):
        return defaults
    merged = dict(defaults)
    if "provider" in raw and isinstance(raw["provider"], str):
        merged["provider"] = raw["provider"]
    if raw.get("model"):
        merged["model"] = str(raw["model"])
    if raw.get("tone"):
        merged["tone"] = str(raw["tone"])[:80]
    if "temperature" in raw:
        merged["temperature"] = _clamp_temperature(raw.get("temperature"))
    if "max_sections" in raw and isinstance(raw["max_sections"], int):
        merged["max_sections"] = max(1, min(int(raw["max_sections"]), 12))
    if "target_chapters" in raw and isinstance(raw["target_chapters"], int):
        merged["target_chapters"] = max(1, min(int(raw["target_chapters"]), 12))
    if "include_images" in raw:
        merged["include_images"] = _boolish(raw["include_images"], defaults["include_images"])
    if raw.get("content_type"):
        merged["content_type"] = str(raw["content_type"]).strip() or defaults.get("content_type")
    if raw.get("instructions"):
        merged["instructions"] = str(raw["instructions"]).strip()
    return merged


def set_ai_preferences(preferences: Dict[str, Any], ai_updates: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new preference blob with ai settings merged."""
    root = dict(preferences or {})
    current = normalize_ai_preferences(root.get("ai"))
    current.update(ai_updates)
    current = normalize_ai_preferences(current)
    root["ai"] = current
    return root


def resolve_generate_blog_options(options: Optional[GenerateBlogOptions], preferences: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve final generation options by merging request payload with user defaults."""
    if options is None:
        options = GenerateBlogOptions()
    prefs = normalize_ai_preferences(preferences.get("ai") if isinstance(preferences, dict) else preferences)
    resolved: Dict[str, Any] = {}

    resolved["tone"] = (options.tone or prefs["tone"]).strip()
    resolved["max_sections"] = int(options.max_sections or prefs["max_sections"])

    target = options.target_chapters or prefs.get("target_chapters") or resolved["max_sections"]
    resolved["target_chapters"] = int(target)

    resolved["include_images"] = bool(
        prefs["include_images"] if options.include_images is None else options.include_images
    )
    resolved["section_index"] = options.section_index
    resolved["model"] = options.model or prefs["model"]
    resolved["temperature"] = (
        _clamp_temperature(options.temperature) if options.temperature is not None else prefs["temperature"]
    )
    resolved["provider"] = prefs.get("provider", "openai")
    resolved["content_type"] = (options.content_type or prefs.get("content_type") or "generic_blog").strip()
    resolved["instructions"] = (options.instructions or prefs.get("instructions") or "").strip() or None
    return resolved
