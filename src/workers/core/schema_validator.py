from __future__ import annotations

from typing import Any, Dict, List, Optional


def validate_schema_json_ld(
    schema_json: Dict[str, Any],
    *,
    schema_type: Optional[str] = None,
    content_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Lightweight schema.org-style validation interface.

    This initial implementation is intentionally conservative:
    - It checks for basic shape issues (missing @context/@type, wrong types).
    - It never raises; it always returns a structured result the caller can surface.

    Future iterations can plug in a richer registry derived from schema.org
    or telemetry from Google Search Console without changing the public
    function signature.
    """
    issues: List[Dict[str, Any]] = []

    if not isinstance(schema_json, dict):
        issues.append(
            {
                "code": "invalid_schema_payload",
                "level": "error",
                "message": "Schema payload must be an object.",
                "path": "$",
                "property": None,
            }
        )
        return {
            "is_valid": False,
            "severity": "error",
            "issues": issues,
            "schema_type": schema_type,
            "hint": content_hint,
            "source": "local",
        }

    ctx = schema_json.get("@context")
    if ctx is None:
        issues.append(
            {
                "code": "missing_context",
                "level": "warning",
                "message": "Missing @context; expected 'https://schema.org'.",
                "path": "$.@context",
                "property": "@context",
            }
        )
    elif isinstance(ctx, str) and "schema.org" not in ctx:
        issues.append(
            {
                "code": "unexpected_context",
                "level": "warning",
                "message": f"Unexpected @context '{ctx}'; typically 'https://schema.org' is used.",
                "path": "$.@context",
                "property": "@context",
            }
        )

    ld_type = schema_json.get("@type")
    if not ld_type:
        issues.append(
            {
                "code": "missing_type",
                "level": "error",
                "message": "Missing @type; search engines require a concrete schema.org type.",
                "path": "$.@type",
                "property": "@type",
            }
        )
    elif not isinstance(ld_type, (str, list)):
        issues.append(
            {
                "code": "invalid_type",
                "level": "error",
                "message": "@type must be a string or list of strings.",
                "path": "$.@type",
                "property": "@type",
            }
        )

    # For FAQ/HowTo/Recipe/Course we can add very light checks that the
    # expected collection keys exist, but avoid being overly strict.
    hint = (content_hint or "").lower()
    if hint in {"faq", "faq_page"}:
        if "mainEntity" not in schema_json:
            issues.append(
                {
                    "code": "faq_missing_main_entity",
                    "level": "warning",
                    "message": "FAQPage schema usually includes 'mainEntity' with Question/Answer pairs.",
                    "path": "$.mainEntity",
                    "property": "mainEntity",
                }
            )
    elif hint in {"how_to", "howto", "how-to"}:
        if "step" not in schema_json:
            issues.append(
                {
                    "code": "howto_missing_steps",
                    "level": "warning",
                    "message": "HowTo schema usually includes a 'step' array of HowToStep items.",
                    "path": "$.step",
                    "property": "step",
                }
            )
    elif hint == "recipe":
        if "recipeInstructions" not in schema_json:
            issues.append(
                {
                    "code": "recipe_missing_instructions",
                    "level": "warning",
                    "message": "Recipe schema usually includes 'recipeInstructions' steps.",
                    "path": "$.recipeInstructions",
                    "property": "recipeInstructions",
                }
            )

    # Determine overall severity.
    has_error = any(issue.get("level") == "error" for issue in issues)
    has_warning = any(issue.get("level") == "warning" for issue in issues)

    if has_error:
        severity = "error"
        is_valid = False
    elif has_warning:
        severity = "warning"
        is_valid = True
    else:
        severity = "ok"
        is_valid = True

    return {
        "is_valid": is_valid,
        "severity": severity,
        "issues": issues,
        "schema_type": schema_type,
        "hint": content_hint,
        "source": "local",
    }
