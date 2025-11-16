"""Helpers for normalizing Google Docs API payloads to and from plain text."""

from __future__ import annotations

from html import escape
from typing import Dict, Any, List


def google_doc_to_text(document: Dict[str, Any]) -> str:
    """Flatten a Google Docs body payload into newline-delimited text."""
    body = document.get("body", {})
    content: List[str] = []
    for element in body.get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        runs = []
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if not text_run:
                continue
            text = text_run.get("content") or ""
            runs.append(text)
        if runs:
            content.append("".join(runs))
    normalized = "\n".join(line.rstrip("\n") for line in content)
    return normalized.strip()


def text_to_html(text: str) -> str:
    """Convert normalized text into simple paragraph-delimited HTML."""
    if not text:
        return ""
    paragraphs = [escape(chunk.strip()) for chunk in text.split("\n") if chunk.strip()]
    if not paragraphs:
        return ""
    return "".join(f"<p>{para}</p>" for para in paragraphs)
