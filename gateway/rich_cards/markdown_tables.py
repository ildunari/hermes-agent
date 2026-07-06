"""Conservative Markdown table auto-capture.

The feature flag is intentionally off by default. This module only upgrades
simple pipe tables with exact line ranges, skips fenced code, and preserves the
source table whenever validation/rendering fails.
"""

from __future__ import annotations

import re
from pathlib import Path

from .renderer import render_card
from .schema import CardRenderResult
from .validate import validate_and_repair

_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def render_markdown_tables_in_response(text: str, *, platform: str = "generic", profile_home: Path | None = None) -> tuple[str, list[CardRenderResult]]:
    candidates = _find_simple_table_ranges(text)
    results: list[CardRenderResult] = []
    if not candidates:
        return text, results
    out: list[str] = []
    last = 0
    for start, end in candidates:
        source = text[start:end]
        parsed = _parse_table(source)
        if parsed is None or not _eligible(parsed):
            continue
        out.append(text[last:start])
        spec, repairs, error, example = validate_and_repair(parsed)
        if spec is None:
            out.append(source)
            last = end
            results.append(CardRenderResult(ok=False, fallback_markdown=source, alt="invalid markdown table", repairs=repairs, error=error, correct_example=example))
            continue
        result = render_card(spec, platform=platform, profile_home=profile_home, fallback=source, repairs=repairs)
        results.append(result)
        if result.ok and result.image_path:
            from .artifacts import _remember_card_media
            _remember_card_media(result.image_path, fallback_markdown=result.fallback_markdown or source, alt=result.alt, force_document=spec.delivery.force_document)
            out.append(f"MEDIA:{result.image_path}")
        else:
            out.append(source)
        last = end
    if not out:
        return text, results
    out.append(text[last:])
    return "".join(out), results


def has_markdown_table_candidate(text: str) -> bool:
    """Return True when the text contains an auto-render-eligible table."""
    for start, end in _find_simple_table_ranges(text or ""):
        parsed = _parse_table((text or "")[start:end])
        if parsed is not None and _eligible(parsed):
            return True
    return False


def _find_simple_table_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    in_fence = False
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence or i + 1 >= len(lines) or not _looks_table_line(lines[i]) or not _SEPARATOR_RE.match(lines[i + 1].strip()):
            i += 1
            continue
        start_i = i
        i += 2
        while i < len(lines) and _looks_table_line(lines[i]):
            i += 1
        ranges.append((offsets[start_i], offsets[i - 1] + len(lines[i - 1])))
    return ranges


def _looks_table_line(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and not stripped.startswith(">")


def _parse_table(source: str) -> dict | None:
    lines = [line.strip() for line in source.strip().splitlines() if line.strip()]
    if len(lines) < 3:
        return None
    columns = _split_row(lines[0])
    rows = [_split_row(line) for line in lines[2:]]
    if len(columns) < 2 or not rows:
        return None
    fixed_rows = [(row + [""] * len(columns))[: len(columns)] for row in rows]
    return {"kind": "table", "columns": columns, "rows": fixed_rows}


def _split_row(line: str) -> list[str]:
    raw = line.strip().strip("|")
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    in_backticks = False
    for ch in raw:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "`":
            in_backticks = not in_backticks
            buf.append(ch)
            continue
        if ch == "|" and not in_backticks:
            cells.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if escaped:
        buf.append("\\")
    cells.append("".join(buf).strip())
    return cells


def _eligible(parsed: dict) -> bool:
    columns = parsed.get("columns") or []
    rows = parsed.get("rows") or []
    width = max((sum(len(str(cell)) for cell in row) for row in rows), default=0) + sum(len(str(c)) for c in columns)
    return len(columns) >= 4 or len(rows) >= 4 or width > 80
