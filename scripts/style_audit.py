#!/usr/bin/env python3
"""
Style Audit Script for Tailwind/Jinja templates (heuristic parser)

Scans ./templates and ./assets/css to inventory Tailwind utility usage,
identify hard-coded color usage, and list component macros with paths.

Heuristic notes and limitations:
- Macro usage detection is based on a regex for `macro_name(` and may still overcount
  in unusual cases. We attempt to avoid counting definitions by checking for a
  preceding `{% macro` prefix near each match, but this is heuristic.
- Variant styles block extraction uses brace counting. We mask string literals
  (single/double-quoted with escapes) before counting braces to avoid braces
  inside strings, but nested braces or complex templating constructs may still
  confuse the parser.

Usage:
  python scripts/style_audit.py --json-out docs/style-audit.json
  python scripts/style_audit.py --json  # print to stdout

Outputs:
- JSON report (recommended for tools/LLMs)
  - Component inventory (macros and files)
  - Tailwind utility frequency (bg-, text-, border-, ring-, shadow-, rounded-, etc.)
  - Unique color scales used (e.g., slate-800, sky-600)
  - Suggested token groups to cover current usage
"""
import argparse
import os
import re
import sys
import json
import datetime
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT / "src" / "workers" / "templates"
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
            rel = str(path.relative_to(ROOT))
            # Skip counting definitions: we only want invocations in templates
            for m in invocation_re.finditer(content):
                macro_name = m.group(1)
                # Heuristic: if directly preceded by a macro definition tag, skip
                start = max(0, m.start() - 50)
                prefix = content[start:m.start()]
                if re.search(r"\{\%\s*macro\s+$", prefix):
                    continue
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
        # Lightweight string masking to avoid counting braces inside strings
        def _mask_strings(line: str) -> str:
            out = []
            in_sq = False
            in_dq = False
            esc = False
            for ch in line:
                if esc:
                    # keep quotes masked context, skip affecting state
                    out.append(' ')
                    esc = False
                    continue
                if ch == '\\':
                    esc = True
                    out.append(' ')
                    continue
                if not in_dq and ch == "'":
                    in_sq = not in_sq
                    out.append(' ')
                    continue
                if not in_sq and ch == '"':
                    in_dq = not in_dq
                    out.append(' ')
                    continue
                if in_sq or in_dq:
                    out.append(' ')
                else:
                    out.append(ch)
            return ''.join(out)
        for line in content.splitlines():
            if not in_block and styles_block_start.search(line):
                in_block = True
                masked = _mask_strings(line)
                open_braces = masked.count("{") - masked.count("}")
                block_lines.append(line)
                continue
            if in_block:
                block_lines.append(line)
                masked = _mask_strings(line)
                open_braces += masked.count("{") - masked.count("}")
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

    # Button uniformity check: robustly find all button macro invocations and check variant usage
    button_usage = defaultdict(lambda: Counter())  # file -> Counter(variants)
    button_invocations_no_variant = []  # files with button() calls without explicit variant
    button_call_ranges = defaultdict(list)  # file -> list of (start_line, end_line)

    def _split_top_level_args(s: str):
        args = []
        buf = []
        in_sq = in_dq = False
        esc = False
        depth = 0
        for ch in s:
            if esc:
                buf.append(ch)
                esc = False
                continue
            if ch == '\\':
                esc = True
                buf.append(ch)
                continue
            if not in_dq and ch == "'":
                in_sq = not in_sq
                buf.append(ch)
                continue
            if not in_sq and ch == '"':
                in_dq = not in_dq
                buf.append(ch)
                continue
            if in_sq or in_dq:
                buf.append(ch)
                continue
            if ch == '(':
                depth += 1
                buf.append(ch)
                continue
            if ch == ')':
                depth = max(0, depth - 1)
                buf.append(ch)
                continue
            if ch == ',' and depth == 0:
                args.append(''.join(buf).strip())
                buf = []
                continue
            buf.append(ch)
        if buf:
            args.append(''.join(buf).strip())
        return args

    for path in iter_files():
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = str(path.relative_to(ROOT))

        # Precompute line start indices for mapping char index -> line number
        lines = content.splitlines(keepends=True)
        line_starts = []
        acc = 0
        for ln in lines:
            line_starts.append(acc)
            acc += len(ln)
        def idx_to_line(idx: int) -> int:
            # binary search
            lo, hi = 0, len(line_starts) - 1
            ans = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if line_starts[mid] <= idx:
                    ans = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return ans + 1  # 1-based

        i = 0
        n = len(content)
        while i < n - 1:
            if content[i] == '{' and content[i+1] == '{':
                j = i + 2
                # skip whitespace
                while j < n and content[j].isspace():
                    j += 1
                # check macro name
                if content[j:j+6] == 'button':
                    k = j + 6
                    while k < n and content[k].isspace():
                        k += 1
                    if k < n and content[k] == '(':
                        # scan until matching '}}' with balanced parens
                        in_sq = in_dq = False
                        esc = False
                        depth = 0
                        end = k
                        while end < n:
                            ch = content[end]
                            if esc:
                                esc = False
                                end += 1
                                continue
                            if ch == '\\':
                                esc = True
                                end += 1
                                continue
                            if not in_dq and ch == "'":
                                in_sq = not in_sq
                                end += 1
                                continue
                            if not in_sq and ch == '"':
                                in_dq = not in_dq
                                end += 1
                                continue
                            if not (in_sq or in_dq):
                                if ch == '(':
                                    depth += 1
                                elif ch == ')':
                                    depth = max(0, depth - 1)
                                # detect end of jinja expr only at depth 0
                                if ch == '}' and end + 1 < n and content[end+1] == '}' and depth == 0:
                                    end += 2
                                    break
                            end += 1
                        call_text = content[i:end]
                        # capture range
                        start_line = idx_to_line(i)
                        end_line = idx_to_line(end - 1)
                        button_call_ranges[rel].append((start_line, end_line))
                        # parse variant
                        # get args substring inside button(...)
                        open_paren = content.find('(', k)
                        close_pos = open_paren + 1
                        # find matching close paren from open_paren
                        in_sq = in_dq = False
                        esc = False
                        depth = 1
                        while close_pos < n:
                            ch = content[close_pos]
                            if esc:
                                esc = False
                                close_pos += 1
                                continue
                            if ch == '\\':
                                esc = True
                                close_pos += 1
                                continue
                            if not in_dq and ch == "'":
                                in_sq = not in_sq
                                close_pos += 1
                                continue
                            if not in_sq and ch == '"':
                                in_dq = not in_dq
                                close_pos += 1
                                continue
                            if not (in_sq or in_dq):
                                if ch == '(':
                                    depth += 1
                                elif ch == ')':
                                    depth -= 1
                                    if depth == 0:
                                        close_pos += 1
                                        break
                            close_pos += 1
                        arg_str = content[open_paren+1:close_pos-1]
                        # search named variant first
                        named = re.search(r"variant\s*=\s*['\"]([^'\"]+)['\"]", arg_str)
                        if named:
                            button_usage[rel][named.group(1)] += 1
                        else:
                            # split top-level args to check positional
                            args = _split_top_level_args(arg_str)
                            if len(args) >= 2:
                                pos2 = args[1].strip()
                                m = re.fullmatch(r"['\"](primary|secondary|destructive|ghost)['\"]", pos2)
                                if m:
                                    button_usage[rel][m.group(1)] += 1
                                else:
                                    snippet = call_text.replace('\n', ' ').strip()[:120]
                                    button_invocations_no_variant.append((rel, snippet))
                            else:
                                snippet = call_text.replace('\n', ' ').strip()[:120]
                                button_invocations_no_variant.append((rel, snippet))
                        i = end
                        continue
                # not a button call
            i += 1

    # Check for raw button elements that should use the macro
    raw_buttons = []  # (file, line_number, snippet)
    button_tag_re = re.compile(r"<\s*button[^>]*>", re.IGNORECASE)
    for path in iter_files():
        try:
            content = path.read_text(encoding="utf-8")
            lines = content.splitlines()
        except Exception:
            continue
        rel = str(path.relative_to(ROOT))
        for i, line in enumerate(lines, 1):
            # skip any line that falls within a captured button macro range
            in_macro = False
            for (sln, eln) in button_call_ranges.get(rel, []):
                if sln <= i <= eln:
                    in_macro = True
                    break
            if in_macro:
                continue
            if button_tag_re.search(line) and "{{ button(" not in line:
                lower = line.lower()
                # Ignore lines that are actually hidden inputs (not buttons)
                if '<input' in lower and 'type="hidden"' in lower:
                    continue
                raw_buttons.append((rel, i, line.strip()[:120]))

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
        "button_usage": {f: dict(c) for f, c in button_usage.items()},
        "button_invocations_no_variant": button_invocations_no_variant,
        "raw_buttons": raw_buttons,
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

    # Button uniformity check
    lines.append("## Button uniformity check")
    button_usage = report.get("button_usage", {})
    if button_usage:
        total_buttons = sum(sum(c.values()) for c in button_usage.values())
        variant_counts = Counter()
        for file_usage in button_usage.values():
            for variant, count in file_usage.items():
                variant_counts[variant] += count
        lines.append(f"- Total button macro calls: {total_buttons}")
        lines.append("- Variant distribution:")
        for variant, count in variant_counts.most_common():
            lines.append(f"  - `{variant}`: {count}")
        lines.append("- Button usage by file:")
        for file, variants in sorted(button_usage.items()):
            total = sum(variants.values())
            lines.append(f"  - {file}: {total} button(s) - {', '.join(f'{v}:{c}' for v, c in variants.items())}")
    else:
        lines.append("- No button macro calls found")
    
    button_no_variant = report.get("button_invocations_no_variant", [])
    if button_no_variant:
        lines.append(f"- ⚠️  {len(button_no_variant)} button call(s) without explicit variant (using default 'primary'):")
        for file, snippet in button_no_variant[:10]:
            lines.append(f"  - {file}: {snippet}")
    
    raw_buttons = report.get("raw_buttons", [])
    if raw_buttons:
        lines.append(f"- ⚠️  {len(raw_buttons)} raw <button> element(s) found (should use button macro):")
        for file, line_num, snippet in raw_buttons[:10]:
            lines.append(f"  - {file}:{line_num}: {snippet}")
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
    lines.append("- Keep components under src/workers/templates/components/elements; avoid inline component patterns in pages.")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=str, help="Write Markdown report to this path")
    ap.add_argument("--print", action="store_true", help="Print report to stdout (Markdown)")
    ap.add_argument("--json", action="store_true", help="Print JSON report to stdout")
    ap.add_argument("--json-out", type=str, help="Write JSON report to this path")
    ap.add_argument("--allowlist", type=str, help="Path to newline-delimited arbitrary allowlist file")
    args = ap.parse_args()

    # Load allowlist if provided
    if args.allowlist:
        p = Path(args.allowlist)
        if p.exists():
            try:
                with p.open("r", encoding="utf-8") as fh:
                    allowed = {line.strip() for line in fh if line.strip()}
                if allowed:
                    ARBITRARY_ALLOWLIST.clear()
                    ARBITRARY_ALLOWLIST.update(allowed)
            except Exception as e:
                print(f"Allowlist load failed for {p}: {e.__class__.__name__}: {e}", file=sys.stderr)

    report = analyze()

    def to_json(report: dict) -> dict:
        # Helper to convert Counter to list of [key, count]
        def c_list(c: Counter):
            return [[k, int(v)] for k, v in c.most_common()]
        # Convert nested counters for macro_usage
        macro_usage = {}
        for f, cnt in report.get("macro_usage", {}).items():
            macro_usage[f] = {k: int(v) for k, v in cnt.items()}
        macro_totals = Counter()
        for cnt in report.get("macro_usage", {}).values():
            for k, v in cnt.items():
                macro_totals[k] += v
        # Variant drift consolidation
        variant_drift = defaultdict(list)
        for file, vmap in report.get("variant_maps", {}).items():
            for variant, classes in vmap.items():
                # We'll aggregate later if drift exists; LLMs can compare
                pass
        data = {
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "root": str(ROOT),
            "path_stats": dict(report.get("path_stats", {})),
            "components": [{"file": f, "macros": ms} for f, ms in report.get("components", [])],
            "macro_usage": macro_usage,
            "macro_totals": {k: int(v) for k, v in macro_totals.most_common()},
            "variant_maps": report.get("variant_maps", {}),
            "util_counters": {cat: c_list(cnt) for cat, cnt in report.get("util_counters", {}).items()},
            "spacing_usage": c_list(report.get("spacing_usage", Counter())),
            "radius_usage": c_list(report.get("radius_usage", Counter())),
            "shadow_usage": c_list(report.get("shadow_usage", Counter())),
            "color_usage": c_list(report.get("color_usage", Counter())),
            "inline_style": {
                "style_tags": report.get("inline_style_files", []),
                "style_attrs": [[f, int(n)] for f, n in report.get("inline_style_attrs", {}).items()],
            },
            "arbitrary": {
                "all": c_list(report.get("util_counters", {}).get("arbitrary", Counter())),
                "allowlist": sorted(list(ARBITRARY_ALLOWLIST)),
                "disallowed": c_list(report.get("arbitrary_disallowed", Counter())),
            },
            "duplicates": [
                {
                    "fingerprint": fp,
                    "count": int(c),
                    "examples": report.get("duplicate_class_examples", {}).get(fp, [])
                }
                for fp, c in sorted(report.get("class_string_fingerprints", {}).items(), key=lambda x: x[1], reverse=True) if c > 1
            ],
            "per_file_summary": {
                f: {
                    "total_classes": int(s.get("total_classes", 0)),
                    "unique_classes": int(s.get("unique_classes", 0)),
                    "top_classes": [[u, int(n)] for u, n in s.get("top_classes", [])],
                }
                for f, s in report.get("per_file_summary", {}).items()
            },
            # Include button-related data for tools/LLMs
            "button_usage": report.get("button_usage", {}),
            "button_invocations_no_variant": report.get("button_invocations_no_variant", []),
            "raw_buttons": report.get("raw_buttons", []),
        }
        return data

    # JSON outputs take precedence if specified
    if args.json or args.json_out:
        data = to_json(report)
        if args.json_out:
            outp = (ROOT / args.json_out).resolve()
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"Wrote {outp}")
        if args.json:
            sys.stdout.write(json.dumps(data))
        return

    # Otherwise, emit Markdown if requested
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
