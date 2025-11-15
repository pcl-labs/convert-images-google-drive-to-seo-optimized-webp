from __future__ import annotations

from typing import Dict, Any, List, Optional
import textwrap
import re
import html


def _coerce_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return str(text).strip()


def generate_outline(text: str, max_sections: int = 5) -> List[Dict[str, str]]:
    """Naive outline generator that splits text into paragraph chunks."""
    clean = _coerce_text(text)
    if not clean:
        return []
    # Ensure positive section count
    try:
        max_sections = int(max_sections)
    except Exception:
        max_sections = 5
    if max_sections < 1:
        max_sections = 1
    paragraphs = [p.strip() for p in clean.split("\n") if p.strip()]
    if not paragraphs:
        paragraphs = [clean]
    chunk_size = max(1, len(paragraphs) // max_sections)
    outline = []
    for idx in range(0, len(paragraphs), chunk_size):
        chunk = paragraphs[idx : idx + chunk_size]
        title = chunk[0][:80]
        summary = textwrap.shorten(" ".join(chunk), width=280, placeholder="…")
        outline.append({"title": title or f"Section {len(outline)+1}", "summary": summary})
        if len(outline) >= max_sections:
            break
    return outline


def organize_chapters(text: str, target_chapters: int = 4) -> List[Dict[str, str]]:
    """Split text into pseudo chapters."""
    clean = _coerce_text(text)
    if not clean:
        return []
    # Ensure positive chapters target
    try:
        target_chapters = int(target_chapters)
    except Exception:
        target_chapters = 4
    if target_chapters <= 0:
        target_chapters = 1
    sentences = [s.strip() for s in clean.replace("!", ".").replace("?", ".").split(".") if s.strip()]
    per_chapter = max(1, len(sentences) // target_chapters)
    chapters: List[Dict[str, str]] = []
    for idx in range(0, len(sentences), per_chapter):
        chunk = sentences[idx : idx + per_chapter]
        if not chunk:
            continue
        title = chunk[0][:70]
        summary = textwrap.shorten(" ".join(chunk), width=360, placeholder="…")
        chapters.append({"title": title or f"Chapter {len(chapters)+1}", "summary": summary})
        if len(chapters) >= target_chapters:
            break
    return chapters


def compose_blog(chapters: List[Dict[str, str]], tone: str = "informative") -> Dict[str, Any]:
    """Create a simple markdown blog post from chapter summaries."""
    if not chapters:
        return {"markdown": "", "meta": {"tone": tone, "word_count": 0}}
    parts = []
    for chap in chapters:
        title = chap.get("title") or "Section"
        summary = chap.get("summary") or ""
        parts.append(f"## {title}\n\n{summary}\n")
    markdown = "\n".join(parts).strip()
    word_count = len(markdown.split())
    return {
        "markdown": markdown,
        "meta": {"tone": tone, "sections": len(chapters), "word_count": word_count},
    }


def default_title_from_outline(outline: List[Dict[str, str]]) -> str:
    if not outline:
        return "Untitled Draft"
    for item in outline:
        title = (item or {}).get("title")
        if title:
            return title[:120]
    return "Untitled Draft"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    cleaned = _SLUG_RE.sub("-", text.lower()).strip("-")
    return cleaned or "post"


def generate_seo_metadata(text: str, outline: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Return lightweight SEO metadata derived from text + outline."""
    base_text = _coerce_text(text)
    title = default_title_from_outline(outline or []) if outline else None
    if not title:
        title = textwrap.shorten(base_text, width=60, placeholder="…") or "Untitled Draft"
    description = textwrap.shorten(base_text, width=180, placeholder="…") if base_text else title
    words = re.findall(r"[a-z0-9]{4,}", base_text.lower())
    keywords: List[str] = []
    for word in words:
        if word not in keywords:
            keywords.append(word)
        if len(keywords) >= 10:
            break
    return {
        "title": title,
        "description": description,
        "slug": _slugify(title),
        "keywords": keywords,
        "hero_image": None,
    }


def generate_image_prompts(chapters: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Produce simple illustrative prompts per chapter for downstream image generation."""
    prompts: List[Dict[str, Any]] = []
    for idx, chapter in enumerate(chapters):
        title = (chapter or {}).get("title") or f"Section {idx + 1}"
        summary = (chapter or {}).get("summary") or ""
        prompts.append(
            {
                "chapter_index": idx,
                "title": title,
                "prompt": f"Create a cinematic wide shot illustrating '{title}' focusing on {summary[:140]}",
                "style": "cinematic",
                "aspect_ratio": "16:9",
            }
        )
    return prompts


def markdown_to_html(markdown_text: str) -> str:
    """Very small markdown-to-HTML converter for headings + paragraphs."""
    lines: List[str] = []
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("### "):
            lines.append(f"<h3>{html.escape(line[4:].strip())}</h3>")
        elif line.startswith("## "):
            lines.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        elif line.startswith("# "):
            lines.append(f"<h1>{html.escape(line[2:].strip())}</h1>")
        else:
            lines.append(f"<p>{html.escape(line)}</p>")
    return "\n".join(lines)
