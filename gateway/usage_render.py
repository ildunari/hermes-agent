"""Compact, cross-surface rendering for the gateway ``/usage`` command."""

from __future__ import annotations

from typing import Any, Mapping


_GAUGE_WIDTH = 10


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _count(value: Any) -> int:
    return max(0, int(_number(value)))


def _compact_count(value: Any) -> str:
    count = _count(value)
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.1f}m".replace(".0m", "m")


def _percent(numerator: Any, denominator: Any) -> float:
    total = _number(denominator)
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, _number(numerator) / total * 100.0))


def _gauge(label: str, numerator: Any, denominator: Any) -> str:
    pct = _percent(numerator, denominator)
    filled = min(_GAUGE_WIDTH, int(pct * _GAUGE_WIDTH / 100.0 + 0.5))
    bar = "█" * filled + "░" * (_GAUGE_WIDTH - filled)
    return (
        f"{label:<8} ▕{bar}▏ {pct:.0f}% "
        f"({_compact_count(numerator)}/{_compact_count(denominator)})"
    )


def _duration(seconds: Any) -> str:
    total = _count(seconds)
    hours, remainder = divmod(total, 3_600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _tool_rows(stats: Mapping[str, Any], max_tools: int) -> list[tuple[str, int, int]]:
    rows: list[tuple[str, int, int]] = []
    for name, raw in (stats or {}).items():
        values = raw if isinstance(raw, Mapping) else {}
        rows.append((str(name), _count(values.get("calls")), _count(values.get("errors"))))
    rows.sort(key=lambda row: (-row[1], -row[2], row[0]))
    return rows[:max_tools]


def _error_label(errors: int) -> str:
    return f"{errors} error" if errors == 1 else f"{errors} errors"


def render_usage_markdown(snapshot: Mapping[str, Any], *, max_tools: int = 5) -> str:
    """Render a usage snapshot as compact Markdown/plain text (under 30 lines)."""
    model = str(snapshot.get("model") or "unknown model")
    provider = str(snapshot.get("provider") or "unknown provider")
    profile = str(snapshot.get("profile") or "default")
    tool_rows = _tool_rows(snapshot.get("tool_stats") or {}, max_tools)

    lines = [
        f"**{model} · {provider} · {profile}**",
        "",
        _gauge("Context", snapshot.get("context_used"), snapshot.get("context_length")),
        _gauge("Cache", snapshot.get("cache_read_tokens"), snapshot.get("prompt_tokens")),
        "",
        f"Tokens in          {_count(snapshot.get('input_tokens')):,}",
        f"Tokens out         {_count(snapshot.get('output_tokens')):,}",
        f"Tokens total       {_count(snapshot.get('total_tokens')):,}",
        f"API calls          {_count(snapshot.get('api_calls')):,}",
        f"Avg output tok/s   {_number(snapshot.get('avg_output_tokens_per_second')):.1f}",
        f"Duration           {_duration(snapshot.get('duration_seconds'))}",
        "",
        "**Tools**",
    ]
    if tool_rows:
        for name, calls, errors in tool_rows:
            suffix = f" · {_error_label(errors)}" if errors else ""
            lines.append(f"{name:<20} {calls:,} call{'s' if calls != 1 else ''}{suffix}")
    else:
        lines.append("No tool calls")
    lines.append(f"Subagents           {_count(snapshot.get('subagent_count')):,}")
    return "\n".join(lines)


def build_usage_card_args(snapshot: Mapping[str, Any], *, max_tools: int = 5) -> dict[str, Any]:
    """Build ``render_message_card`` arguments from the same usage snapshot."""
    context_pct = _percent(snapshot.get("context_used"), snapshot.get("context_length"))
    cache_pct = _percent(snapshot.get("cache_read_tokens"), snapshot.get("prompt_tokens"))
    tool_items = [
        {
            "label": name,
            "value": f"{calls:,}",
            "detail": f"{calls:,} call{'s' if calls != 1 else ''} · {_error_label(errors)}",
        }
        for name, calls, errors in _tool_rows(snapshot.get("tool_stats") or {}, max_tools)
    ]
    tool_items.append({
        "label": "Subagents",
        "value": str(_count(snapshot.get("subagent_count"))),
        "detail": "delegate_task calls",
    })
    return {
        "kind": "metric_grid",
        "subtitle": " · ".join((
            str(snapshot.get("model") or "unknown model"),
            str(snapshot.get("provider") or "unknown provider"),
            str(snapshot.get("profile") or "default"),
        )),
        "metrics": [
            {"label": "Context", "value": f"{context_pct:.0f}%", "detail": f"{_compact_count(snapshot.get('context_used'))} / {_compact_count(snapshot.get('context_length'))}"},
            {"label": "Cache hit", "value": f"{cache_pct:.0f}%", "detail": f"{_compact_count(snapshot.get('cache_read_tokens'))} read"},
            {"label": "Tokens", "value": f"{_count(snapshot.get('total_tokens')):,}", "detail": f"{_count(snapshot.get('input_tokens')):,} in · {_count(snapshot.get('output_tokens')):,} out"},
            {"label": "API calls", "value": f"{_count(snapshot.get('api_calls')):,}"},
            {"label": "Output speed", "value": f"{_number(snapshot.get('avg_output_tokens_per_second')):.1f} tok/s"},
            {"label": "Duration", "value": _duration(snapshot.get("duration_seconds"))},
        ],
        "items": tool_items,
        "style": {"theme": "auto", "look": "dashboard", "density": "compact", "width": 720},
    }
