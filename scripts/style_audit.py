#!/usr/bin/env python3
"""
Style Audit Script for Tailwind/Jinja templates

Scans ./templates and ./assets/css to inventory Tailwind utility usage,
identify hard-coded color usage, and list component macros with paths.

Usage:
  python scripts/style_audit.py --out docs/style-audit.md
  python scripts/style_audit.py --print

Outputs a Markdown report with:
- Component inventory (macros and files)
- Tailwind utility frequency (bg-, text-, border-, ring-, shadow-, rounded-, etc.)
- Unique color scales used (e.g., slate-800, sky-600)
- Suggested token groups to cover current usage
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT / "templates"
ASSETS_DIR = ROOT / "assets"

CLASS_RE = re.compile(r'class\s*=\s*"([^"]+)"')
MACRO_RE = re.compile(r"\{\%\s*macro\s+([a-zA-Z_][a-zA-Z0-9_]*)")
STYLE_ATTR_RE = re.compile(r'style\s*=\s*"([^"]+)"')
STYLE_TAG_RE = re.compile(r"<\s*style[\s>]", re.IGNORECASE)

# Utility category patterns
PATTERNS = {
    "bg": re.compile(r"^bg-"),
    "text": re.compile(r"^text-"),
    "border": re.compile(r"^border-"),
    "ring": re.compile(r"^ring-"),
    "shadow": re.compile(r"^shadow"),
    "rounded": re.compile(r"^rounded"),
    "spacing": re.compile(r"^(p|px|py|pt|pr|pb|pl|m|mx|my|mt|mr|mb|ml)-"),
    "opacity": re.compile(r"^opacity-"),
    "divide": re.compile(r"^divide-"),
    "outline": re.compile(r"^outline"),
}

# Color extraction patterns (e.g., slate-800, sky-600, rose-500, etc.)
COLOR_TOKEN_RE = re.compile(r"^(?:bg|text|border|ring|divide)-(slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-(\d{2,3})")
HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
RGB_COLOR_RE = re.compile(r"\b(?:rgb|rgba)\s*\(")
ARBITRARY_TW_VALUE_RE = re.compile(r"[\[\]]")  # detects classes like bg-[...], p-[...] etc

# Arbitrary utility allowlist (acceptable custom values)
ARBITRARY_ALLOWLIST = {
    "min-w-[720px]",
}

# Files we scan
SCAN_EXTS = {".html", ".htm"}


def iter_files():
    # templates
    for base in [TEMPLATES_DIR]:
        if not base.exists():
            continue
        for root, _, files in os.walk(base):
            for f in files:
                p = Path(root) / f
                if p.suffix in SCAN_EXTS:
                    yield p


def extract_classes(text: str):
    tokens = []
    for m in CLASS_RE.finditer(text):
        raw = m.group(1)
        # naive split by whitespace; ignores template conditionals inside
        for t in re.split(r"\s+", raw.strip()):
            t = t.strip()
            if t:
                tokens.append(t)
    return tokens


def extract_macros(text: str):
    return MACRO_RE.findall(text)


def analyze():
    component_index = []  # list of (file, [macros])
    util_counters = {k: Counter() for k in PATTERNS}
    all_utils = Counter()
    color_usage = Counter()
    spacing_usage = Counter()
    radius_usage = Counter()
    shadow_usage = Counter()
    per_file_summary = {}
    path_stats = Counter()  # directory -> file count
    class_string_fingerprints = Counter()  # normalized class strings -> occurrences
    duplicate_class_examples = defaultdict(list)  # fingerprint -> [(file, snippet)]
    inline_style_files = set()
    inline_style_attrs = Counter()

    # First pass: gather macros and per-file class data
    for path in iter_files():
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue

        macros = extract_macros(content)
        if macros:
            component_index.append((str(path.relative_to(ROOT)), macros))

        rel = str(path.relative_to(ROOT))
        path_stats[str(path.parent.relative_to(ROOT))] += 1

        # detect inline <style> tags and style="..." attrs
        if STYLE_TAG_RE.search(content):
            inline_style_files.add(rel)

        classes = extract_classes(content)
        if classes:
            # collect per-file stats
            file_utils = Counter()
            for cls in classes:
                file_utils[cls] += 1
            per_file_summary[rel] = {
                "total_classes": len(classes),
                "unique_classes": len(set(classes)),
                "top_classes": file_utils.most_common(10),
            }

        for cls in classes:
            # Count overall utility frequency
            all_utils[cls] += 1
            # Categorize
            for cat, rx in PATTERNS.items():
                if rx.match(cls):
                    util_counters[cat][cls] += 1
            # Colors
            m = COLOR_TOKEN_RE.match(cls)
            if m:
                color_usage[f"{m.group(1)}-{m.group(2)}"] += 1
            # Radius utilities
            if cls.startswith("rounded"):
                radius_usage[cls] += 1
            # Shadows
            if cls.startswith("shadow"):
                shadow_usage[cls] += 1
            # Spacing
            if PATTERNS["spacing"].match(cls):
                spacing_usage[cls] += 1
            # Arbitrary Tailwind classes
            if ARBITRARY_TW_VALUE_RE.search(cls):
                util_counters.setdefault("arbitrary", Counter())[cls] += 1

        # Inline style attributes at token level
        for sm in STYLE_ATTR_RE.finditer(content):
            inline_style_attrs[rel] += 1

        # Duplicate class string detection: capture normalized quoted class strings
        for m in CLASS_RE.finditer(content):
            raw = m.group(1).strip()
            # normalize: split, dedup order-insensitively by sorting
            parts = [t for t in re.split(r"\s+", raw) if t]
            if not parts:
                continue
            fingerprint = " ".join(sorted(parts))
            class_string_fingerprints[fingerprint] += 1
            if len(duplicate_class_examples[fingerprint]) < 3:
                snippet = raw[:120]
                duplicate_class_examples[fingerprint].append((rel, snippet))

    # Second pass: component usage graph (macro invocations across templates)
    # Build a list of known macro names from component_index
    known_macros = set()
    macro_file_map = defaultdict(list)  # macro -> [files where defined]
    for file, macros in component_index:
        for m in macros:
            known_macros.add(m)
            macro_file_map[m].append(file)

    macro_usage = defaultdict(lambda: Counter())  # file -> Counter(macros)
    invocation_re = re.compile(r"\b(" + "|".join(re.escape(m) for m in sorted(known_macros)) + r")\s*\(") if known_macros else None
    if invocation_re:
        for path in iter_files():
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                continue
            # Skip counting definitions: we only want invocations in templates
            for m in invocation_re.finditer(content):
                macro_name = m.group(1)
                rel = str(path.relative_to(ROOT))
                macro_usage[rel][macro_name] += 1

    # Variant drift check: look for `{% set styles = { 'primary': '...' } %}` maps in components
    variant_maps = {}  # file -> {variant -> classes}
    styles_block_start = re.compile(r"\{\%\s*set\s+styles\s*=\s*\{")
    variant_entry_re = re.compile(r"['\"]([a-zA-Z0-9_\-]+)['\"]\s*:\s*['\"]([^'\"]+)['\"]")
    for file, _ in component_index:
        p = ROOT / file
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if not styles_block_start.search(content):
            continue
        # Extract the block heuristically until the closing `}` of the set
        block_lines = []
        open_braces = 0
        in_block = False
        for line in content.splitlines():
            if not in_block and styles_block_start.search(line):
                in_block = True
                open_braces = line.count("{") - line.count("}")
                block_lines.append(line)
                continue
            if in_block:
                block_lines.append(line)
                open_braces += line.count("{") - line.count("}")
                if open_braces <= 0 and "}%" in line:
                    break
        if block_lines:
            block = "\n".join(block_lines)
            vmap = {}
            for m in variant_entry_re.finditer(block):
                vmap[m.group(1)] = m.group(2)
            if vmap:
                variant_maps[file] = vmap

    # Separate allowed vs disallowed arbitrary utilities
    arbitrary_all = util_counters.get("arbitrary", Counter())
    arbitrary_disallowed = Counter()
    for k, v in arbitrary_all.items():
        if k not in ARBITRARY_ALLOWLIST:
            arbitrary_disallowed[k] = v

    return {
        "components": component_index,
        "util_counters": util_counters,
        "all_utils": all_utils,
        "color_usage": color_usage,
        "spacing_usage": spacing_usage,
        "radius_usage": radius_usage,
        "shadow_usage": shadow_usage,
        "per_file_summary": per_file_summary,
        "path_stats": path_stats,
        "class_string_fingerprints": class_string_fingerprints,
        "duplicate_class_examples": duplicate_class_examples,
        "inline_style_files": sorted(inline_style_files),
        "inline_style_attrs": inline_style_attrs,
        "macro_usage": macro_usage,
        "macro_file_map": macro_file_map,
        "variant_maps": variant_maps,
        "arbitrary_disallowed": arbitrary_disallowed,
    }


def suggested_tokens(color_counts: Counter):
    # Map popular utility color hues to semantic roles (heuristic)
    # We will suggest tokens for top hues in use.
    hue_totals = defaultdict(int)  # hue -> total count across scales
    for hue_scale, count in color_counts.items():
        hue = hue_scale.split("-")[0]
        hue_totals[hue] += count

    top_hues = [h for h, _ in sorted(hue_totals.items(), key=lambda x: x[1], reverse=True)[:4]]
    roles = [
        ("primary", None),
        ("destructive", None),
        ("surface", None),
        ("accent", None),
    ]
    suggestions = {}
    for (role, _), hue in zip(roles, top_hues):
        suggestions[role] = hue
    # Always propose neutral slate/gray for base
    if "surface" not in suggestions:
        suggestions["surface"] = "slate"
    return suggestions


def render_markdown(report: dict) -> str:
    components = report["components"]
    util_counters = report["util_counters"]
    color_usage = report["color_usage"]
    spacing_usage = report["spacing_usage"]
    radius_usage = report["radius_usage"]
    shadow_usage = report["shadow_usage"]
    per_file_summary = report["per_file_summary"]
    path_stats = report["path_stats"]
    class_string_fingerprints = report["class_string_fingerprints"]
    duplicate_class_examples = report["duplicate_class_examples"]
    inline_style_files = report["inline_style_files"]
    inline_style_attrs = report["inline_style_attrs"]
    macro_usage = report["macro_usage"]
    macro_file_map = report["macro_file_map"]
    variant_maps = report["variant_maps"]
    arbitrary_disallowed = report["arbitrary_disallowed"]

    lines = []
    lines.append("# Style Audit Report")
    lines.append("")

    # Component usage graph
    lines.append("## Component usage graph (macro invocations)")
    if macro_usage:
        # Top macros by total usage
        totals = Counter()
        for f, cnt in macro_usage.items():
            for m, n in cnt.items():
                totals[m] += n
        lines.append("- **Top macros**")
        for m, n in totals.most_common():
            defs = ", ".join(macro_file_map.get(m, []))
            lines.append(f"  - `{m}`: {n} uses (defined in: {defs})")
        lines.append("- **Top files invoking macros**")
        for f, cnt in sorted(macro_usage.items(), key=lambda x: sum(x[1].values()), reverse=True)[:15]:
            lines.append(f"  - {f} -> {sum(cnt.values())} calls: " + ", ".join(f"{m}:{n}" for m, n in cnt.most_common()))
    else:
        lines.append("- No macro invocations detected")
    lines.append("")

    # Variant drift check
    lines.append("## Variant drift check (styles map)")
    if variant_maps:
        # Collect union of all variants
        all_variants = set()
        for vmap in variant_maps.values():
            all_variants.update(vmap.keys())
        lines.append("- Variants considered: " + ", ".join(sorted(all_variants)))
        # For each variant, list distinct class strings used across files
        for variant in sorted(all_variants):
            seen = defaultdict(list)
            for file, vmap in variant_maps.items():
                if variant in vmap:
                    seen[vmap[variant]].append(file)
            if len(seen) > 1:
                lines.append(f"- Drift for `{variant}`:")
                for cls, files in seen.items():
                    lines.append(f"  - classes: " + cls)
                    for f in files:
                        lines.append(f"    - {f}")
            else:
                lines.append(f"- `{variant}`: consistent across {len(next(iter(seen.values()))) if seen else 0} file(s)")
    else:
        lines.append("- No styles map blocks found in components")
    lines.append("")

    # Components
    lines.append("## Component inventory (Jinja macros)")
    if not components:
        lines.append("- No macros found.")
    else:
        for file, macros in sorted(components):
            lines.append(f"- {file}")
            for m in macros:
                lines.append(f"  - macro: `{m}`")
    lines.append("")

    # Path organization
    lines.append("## Path organization")
    if path_stats:
        for d, n in path_stats.most_common():
            lines.append(f"- {d or '.'}: {n} files")
    lines.append("")

    # Utility categories
    lines.append("## Tailwind utility usage by category (top 20)")
    for cat in ["bg", "text", "border", "ring", "shadow", "rounded", "opacity", "divide", "outline"]:
        cnt = util_counters.get(cat, Counter())
        lines.append(f"- **{cat}**")
        for util, n in cnt.most_common(20):
            lines.append(f"  - {util}: {n}")
    lines.append("")

    # Spacing, radius, shadow details
    lines.append("## Spacing utilities (top 20)")
    for util, n in spacing_usage.most_common(20):
        lines.append(f"- {util}: {n}")
    lines.append("")
    lines.append("## Radius utilities (top 20)")
    for util, n in radius_usage.most_common(20):
        lines.append(f"- {util}: {n}")
    lines.append("")
    lines.append("## Shadow utilities (top 20)")
    for util, n in shadow_usage.most_common(20):
        lines.append(f"- {util}: {n}")
    lines.append("")

    # Colors
    lines.append("## Color scales in use (hue-scale)")
    if color_usage:
        for hue_scale, n in color_usage.most_common():
            lines.append(f"- {hue_scale}: {n}")
    else:
        lines.append("- None detected")
    lines.append("")

    # Arbitrary Tailwind and inline styles
    lines.append("## Arbitrary Tailwind values in use")
    arb = util_counters.get("arbitrary") or Counter()
    if arb:
        for util, n in arb.most_common(30):
            lines.append(f"- {util}: {n}")
    else:
        lines.append("- None detected")
    lines.append("")

    lines.append("### Disallowed arbitrary Tailwind utilities (not in allowlist)")
    if arbitrary_disallowed:
        for util, n in arbitrary_disallowed.most_common(30):
            lines.append(f"- {util}: {n}")
    else:
        lines.append("- None (all arbitrary values are in allowlist)")
    lines.append("")

    lines.append("## Inline <style> tags and style attributes")
    if inline_style_files:
        lines.append("- Files with <style> tag:")
        for f in inline_style_files:
            lines.append(f"  - {f}")
    else:
        lines.append("- No <style> tags detected")
    if inline_style_attrs:
        lines.append("- Files with style="" attributes (counts):")
        for f, cnt in sorted(inline_style_attrs.items(), key=lambda x: x[1], reverse=True)[:20]:
            lines.append(f"  - {f}: {cnt}")
    else:
        lines.append("- No inline style attributes detected")
    lines.append("")

    # Duplicate class strings across files
    lines.append("## Duplicate class strings (potential utility composition candidates)")
    dups = [(fp, c) for fp, c in class_string_fingerprints.items() if c > 1]
    dups.sort(key=lambda x: x[1], reverse=True)
    if dups:
        for fp, c in dups[:30]:
            lines.append(f"- Occurrences: {c}")
            for (f, snippet) in duplicate_class_examples.get(fp, [])[:3]:
                lines.append(f"  - {f}: \"{snippet}\"")
    else:
        lines.append("- No duplicates found above threshold")
    lines.append("")

    # Token suggestions
    suggestions = suggested_tokens(color_usage)
    lines.append("## Suggested token mapping (initial heuristic)")
    for role, hue in suggestions.items():
        lines.append(f"- `{role}` -> `{hue}` (review)")
    lines.append("")

    # Next steps
    lines.append("## Next steps")
    lines.append("- Confirm token roles and hues based on brand.")
    lines.append("- Map tokens in tailwind.config.js and CSS variables.")
    lines.append("- Refactor components starting with buttons and cards.")

    # Recommendations summary
    lines.append("")
    lines.append("## Recommendations summary")
    lines.append("- Consolidate repeated class strings into semantic utilities in @layer components.")
    lines.append("- Replace arbitrary Tailwind values with token-based classes where possible.")
    lines.append("- Eliminate inline style attributes and <style> tags in templates.")
    lines.append("- Normalize spacing and radius scale usage; prefer small set (md, lg).")
    lines.append("- Keep components under templates/components/elements; avoid inline component patterns in pages.")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=str, help="Write Markdown report to this path")
    ap.add_argument("--print", action="store_true", help="Print report to stdout")
    args = ap.parse_args()

    report = analyze()
    md = render_markdown(report)

    if args.out:
        out_path = (ROOT / args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        print(f"Wrote {out_path}")
    if args.print or not args.out:
        sys.stdout.write(md + "\n")


if __name__ == "__main__":
    main()
