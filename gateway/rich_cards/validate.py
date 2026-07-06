"""Validation, safe repair, and fallback formatting for rich message cards."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from .schema import MessageCardSpec


_KIND_EXAMPLES: dict[str, dict[str, Any]] = {
    "table": {
        "kind": "table",
        "title": "Example table",
        "columns": ["Name", "Value"],
        "rows": [["A", "10"], ["B", "20"]],
    },
    "chart": {
        "kind": "chart",
        "title": "Example chart",
        "chart": {
            "type": "bar",
            "labels": ["GPT", "Claude"],
            "series": [{"name": "Usage", "values": [10, 20]}],
        },
    },
    "metric_grid": {
        "kind": "metric_grid",
        "title": "Example metrics",
        "metrics": [{"label": "Tests", "value": "142", "status": "pass"}],
    },
    "status": {
        "kind": "status",
        "title": "Build status",
        "columns": ["Check", "Result", "Notes"],
        "rows": [["Unit tests", "Pass", "142 tests"]],
    },
    "receipt": {
        "kind": "receipt",
        "title": "Receipt",
        "items": [
            {"label": "Plan", "qty": 1, "amount": "$20.00"},
            {"label": "Tax", "amount": "$1.60"},
        ],
        "data": {"total": "$21.60"},
    },
}


def validate_and_repair(raw: dict[str, Any]) -> tuple[MessageCardSpec | None, list[str], str | None, dict[str, Any] | None]:
    """Return a validated spec, repairs, error, and corrected example.

    Repairs are intentionally narrow and data-preserving: shape coercions,
    numeric string conversion for chart values, table row padding, and style
    defaults. We do not guess missing chart labels, units, or semantic values.
    """
    repairs: list[str] = []
    data = deepcopy(raw or {})
    if not isinstance(data, dict):
        return None, repairs, "message card body must be an object", _example("table")

    kind = str(data.get("kind") or "").strip().lower()
    if kind == "chart-card":
        data["kind"] = "chart"
        repairs.append("Mapped chart-card alias to kind: chart")
    elif kind:
        data["kind"] = kind

    style = data.get("style")
    if isinstance(style, dict):
        if style.get("theme") not in (None, "auto", "dark", "light"):
            style["theme"] = "auto"
            repairs.append("Invalid style.theme defaulted to auto")
        if style.get("look") not in (None, "auto", "default", "dashboard", "minimal", "editorial", "terminal", "receipt", "status"):
            style["look"] = "auto"
            repairs.append("Invalid style.look defaulted to auto")
        if style.get("density") not in (None, "compact", "normal", "roomy"):
            style["density"] = "normal"
            repairs.append("Invalid style.density defaulted to normal")
        accent = style.get("accent")
        if accent is not None and not _is_safe_hex_color(str(accent)):
            style.pop("accent", None)
            repairs.append("Invalid style.accent removed; use #RGB or #RRGGBB")

    _repair_table_rows(data, repairs)
    _repair_chart_values(data, repairs)
    _repair_status_items(data, repairs)

    try:
        return MessageCardSpec.model_validate(data), repairs, None, None
    except ValidationError as exc:
        return None, repairs, _human_validation_error(exc), _example(data.get("kind"))


def _is_safe_hex_color(value: str) -> bool:
    value = value.strip()
    if not value.startswith("#"):
        return False
    raw = value[1:]
    return len(raw) in {3, 6} and all(char in "0123456789abcdefABCDEF" for char in raw)


def _repair_table_rows(data: dict[str, Any], repairs: list[str]) -> None:
    rows = data.get("rows")
    columns = data.get("columns")

    if isinstance(rows, dict):
        rows = [rows]
        data["rows"] = rows
        repairs.append("Converted single row object to a one-row table")

    if isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows):
        if not columns:
            keys: list[str] = []
            for row in rows:
                for key in row.keys():
                    if key not in keys:
                        keys.append(str(key))
            columns = keys
            data["columns"] = columns
            repairs.append("Inferred table columns from object row keys")
        data["rows"] = [[row.get(col, "") for col in columns] for row in rows]
        repairs.append("Converted object rows to ordered table rows")
        rows = data["rows"]

    if isinstance(columns, list) and isinstance(rows, list):
        max_len = max((len(row) if isinstance(row, list) else 1 for row in rows), default=len(columns))
        if max_len > len(columns):
            extra_count = max_len - len(columns)
            columns.extend(f"Extra {i + 1}" for i in range(extra_count))
            data["columns"] = columns
            repairs.append("Added extra columns instead of dropping long row cells")
        repaired_rows: list[list[Any]] = []
        changed = False
        for row in rows:
            if not isinstance(row, list):
                row = [row]
                changed = True
            if len(row) < len(columns):
                row = row + [""] * (len(columns) - len(row))
                changed = True
            repaired_rows.append(row)
        if changed:
            data["rows"] = repaired_rows
            repairs.append("Normalized table row lengths to match columns")


def _repair_chart_values(data: dict[str, Any], repairs: list[str]) -> None:
    chart = data.get("chart")
    if not isinstance(chart, dict):
        return
    series = chart.get("series")
    if isinstance(series, dict):
        series = [series]
        chart["series"] = series
        repairs.append("Converted chart.series object to a list")
    if not isinstance(series, list):
        return
    for idx, item in enumerate(series):
        if not isinstance(item, dict):
            continue
        values = item.get("values")
        if isinstance(values, str):
            try:
                item["values"] = [float(part.strip()) for part in values.split(",") if part.strip()]
                repairs.append(f"Converted chart.series[{idx}].values CSV string to numbers")
            except ValueError:
                continue
        elif isinstance(values, list):
            converted = []
            changed = False
            for value in values:
                if isinstance(value, str):
                    try:
                        converted.append(float(value))
                        changed = True
                    except ValueError:
                        converted.append(value)
                else:
                    converted.append(value)
            if changed:
                item["values"] = converted
                repairs.append(f"Converted numeric strings in chart.series[{idx}].values to numbers")


def _repair_status_items(data: dict[str, Any], repairs: list[str]) -> None:
    if data.get("kind") != "status" or data.get("rows") or not data.get("items"):
        return
    items = data.get("items")
    if isinstance(items, list) and all(isinstance(item, dict) for item in items):
        data["columns"] = data.get("columns") or ["Item", "Status", "Notes"]
        data["rows"] = [
            [item.get("label") or item.get("name") or "", item.get("status") or item.get("result") or "", item.get("note") or item.get("notes") or ""]
            for item in items
        ]
        repairs.append("Converted status items to table rows")


def fallback_markdown(spec_or_data: MessageCardSpec | dict[str, Any], original: str | None = None) -> str:
    if original:
        return original.strip()
    data = spec_or_data.model_dump(mode="json", exclude_none=True) if isinstance(spec_or_data, MessageCardSpec) else spec_or_data
    kind = data.get("kind", "card")
    title = data.get("title")
    lines: list[str] = []
    if title:
        lines.append(f"**{title}**")
    if data.get("subtitle"):
        lines.append(str(data["subtitle"]))
    columns = data.get("columns")
    rows = data.get("rows")
    if columns and rows:
        lines.append("| " + " | ".join(map(str, columns)) + " |")
        lines.append("| " + " | ".join(["---"] * len(columns)) + " |")
        for row in rows:
            cells = list(row) if isinstance(row, list) else [row]
            lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    elif kind == "chart" and data.get("chart"):
        chart = data["chart"]
        labels = chart.get("labels") or []
        series = chart.get("series") or []
        for s in series:
            name = s.get("name") or "Series"
            values = s.get("values") or []
            lines.append(f"- {name}: " + ", ".join(f"{labels[i] if i < len(labels) else i}: {v}" for i, v in enumerate(values)))
    elif kind == "receipt" and data.get("items"):
        for item in data["items"]:
            label = item.get("label") or item.get("name") or item.get("description") or "Item"
            qty = item.get("qty") or item.get("quantity")
            amount = item.get("amount") or item.get("price") or item.get("total") or ""
            prefix = f"{qty} × " if qty not in (None, "") else ""
            lines.append(f"- {prefix}{label}: {amount}".rstrip())
        totals = data.get("data") or {}
        for key in ("subtotal", "tax", "tip", "total"):
            if key in totals:
                lines.append(f"- {key.title()}: {totals[key]}")
    elif data.get("metrics"):
        for metric in data["metrics"]:
            lines.append(f"- {metric.get('label')}: {metric.get('value')}")
    if not lines:
        lines.append(f"[{kind} card unavailable]")
    return "\n".join(lines)


def _human_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {"msg": str(exc), "loc": []}
    loc = ".".join(str(part) for part in first.get("loc", []) if part != "__root__")
    prefix = f"{loc}: " if loc else ""
    return prefix + str(first.get("msg", "invalid message card"))


def _example(kind: Any) -> dict[str, Any]:
    key = str(kind or "table")
    return deepcopy(_KIND_EXAMPLES.get(key, _KIND_EXAMPLES["table"]))
