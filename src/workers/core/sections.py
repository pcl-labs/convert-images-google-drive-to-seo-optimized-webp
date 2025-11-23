from __future__ import annotations

from typing import Any, Dict, List, Tuple, Optional
import json
import re

from api.database import get_document, get_document_version


def _coerce_sections(raw: Any) -> List[Dict[str, Any]]:
    """Best-effort conversion of a version_row['sections'] value into a list of dicts.

    Accepts:
    - JSON-encoded string
    - list of dicts
    - None / other types (treated as empty)
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        if isinstance(parsed, list):
            return [s for s in parsed if isinstance(s, dict)]
        return []
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    return []


def normalize_sections(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ensure each section has a stable section_id and index.

    - Adds an integer 0-based ``index`` field when missing.
    - Preserves existing numeric ``index`` when reasonable.
    - Preserves any existing ``section_id``; otherwise assigns ``sec-{index}``.
    - Does NOT change semantic content fields like title/summary/image_prompt.
    """
    normalized: List[Dict[str, Any]] = []

    for idx, raw in enumerate(sections or []):
        if not isinstance(raw, dict):
            # Skip non-dict entries defensively
            continue
        section = dict(raw)

        # Derive a 0-based index; prefer explicit index/order if present
        index_value = section.get("index")
        if not isinstance(index_value, int):
            order_value = section.get("order")
            if isinstance(order_value, int):
                index_value = order_value
            else:
                index_value = idx
        # Clamp to non-negative
        if index_value < 0:
            index_value = 0
        section["index"] = index_value

        # Stable section_id: keep existing if present and non-empty
        section_id = section.get("section_id")
        if not isinstance(section_id, str) or not section_id.strip():
            section_id = f"sec-{index_value}"
        section["section_id"] = section_id

        normalized.append(section)

    return normalized


def extract_sections_from_version(version_row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse and normalize sections from a document_versions row.

    ``version_row['sections']`` may be a JSON string or already a list.
    This helper always returns a list of normalized section dicts.
    """
    raw_sections = version_row.get("sections")
    coerced = _coerce_sections(raw_sections)
    return normalize_sections(coerced)


def find_section_by_id(sections: List[Dict[str, Any]], section_id: str) -> Tuple[int, Dict[str, Any]]:
    """Return (index, section_dict) for the matching section_id.

    Sections are normalized on the fly to ensure ``section_id``/``index`` exist.
    Raises ``KeyError`` if no matching section_id is found.
    """
    if not isinstance(section_id, str) or not section_id:
        raise KeyError("section_id must be a non-empty string")

    normalized = normalize_sections(sections)
    for idx, section in enumerate(normalized):
        if section.get("section_id") == section_id:
            return idx, section

    raise KeyError(f"Section with id '{section_id}' not found")


def _word_count(text: str) -> int:
    """Return a simple whitespace-based word count for a text string.

    Used by API layers for lightweight section statistics; not persisted.
    """
    if not text:
        return 0
    return len(str(text).split())


def extract_sections_from_mdx(body_mdx: str) -> List[Dict[str, Any]]:
    """Extract sections from MDX content by parsing H2 and H3 headings.
    
    Returns a list of section dicts with:
    - section_id: stable identifier (sec-0, sec-1, etc.)
    - index: 0-based index
    - title: heading text
    - summary: first paragraph or excerpt after heading (up to 200 chars)
    - body_mdx: content between this heading and next (or end)
    """
    if not body_mdx:
        return []
    
    sections: List[Dict[str, Any]] = []
    lines = body_mdx.splitlines()
    current_section: Optional[Dict[str, Any]] = None
    current_content: List[str] = []
    
    for line in lines:
        stripped = line.strip()
        
        # Check for H2 or H3 heading
        h2_match = re.match(r"^##\s+(.+)$", stripped)
        h3_match = re.match(r"^###\s+(.+)$", stripped)
        
        if h2_match or h3_match:
            # Save previous section if exists
            if current_section is not None:
                body_text = "\n".join(current_content).strip()
                # Extract summary from first paragraph
                summary = ""
                if body_text:
                    # Get first paragraph or first 200 chars
                    first_para = body_text.split("\n\n")[0] if "\n\n" in body_text else body_text
                    summary = first_para[:200].strip()
                    if len(first_para) > 200:
                        summary += "..."
                current_section["summary"] = summary
                current_section["body_mdx"] = body_text
                sections.append(current_section)
            
            # Start new section
            title = (h2_match or h3_match).group(1).strip()
            idx = len(sections)
            current_section = {
                "section_id": f"sec-{idx}",
                "index": idx,
                "title": title,
                "summary": "",
                "body_mdx": "",
            }
            current_content = []
        else:
            # Add to current section content
            if current_section is not None:
                current_content.append(line)
            elif not sections:
                # Content before first heading - create intro section
                if stripped:
                    current_content.append(line)
    
    # Save last section
    if current_section is not None:
        body_text = "\n".join(current_content).strip()
        summary = ""
        if body_text:
            first_para = body_text.split("\n\n")[0] if "\n\n" in body_text else body_text
            summary = first_para[:200].strip()
            if len(first_para) > 200:
                summary += "..."
        current_section["summary"] = summary
        current_section["body_mdx"] = body_text
        sections.append(current_section)
    elif current_content:
        # No headings found, create single section from all content
        body_text = "\n".join(current_content).strip()
        summary = body_text[:200].strip()
        if len(body_text) > 200:
            summary += "..."
        sections.append({
            "section_id": "sec-0",
            "index": 0,
            "title": "Content",
            "summary": summary,
            "body_mdx": body_text,
        })
    
    return normalize_sections(sections)


async def get_latest_version_for_project(
    db: Any,
    project: Dict[str, Any],
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the latest document_versions row for a project's document.

    Looks up the associated document row to read ``latest_version_id`` and,
    if present, resolves it via ``get_document_version``. Returns ``None``
    when the project has no backing document or no versions yet.
    """
    document_id = project.get("document_id")
    if not document_id:
        return None

    doc = await get_document(db, document_id, user_id=user_id)
    if not doc:
        return None

    latest_version_id = doc.get("latest_version_id")
    if not latest_version_id:
        return None

    version_row = await get_document_version(db, document_id, latest_version_id, user_id)
    return version_row or None
