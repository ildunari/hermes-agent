#!/usr/bin/env python3
"""Read-only, local audit of persisted Hermes tool workloads.

The default path never loads the live tool registry and never invokes availability
checks. Pass --check-availability or candidate toolsets explicitly to opt into
loading registered tools; only --check-availability executes their existing
check_fn callables. The command never writes config or the selected state DB.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


@dataclass(frozen=True)
class PersistedCall:
    session_id: str
    tool_call_id: str | None
    tool_name: str
    timestamp: float
    row_identity: str = ""


DEFERRED_BRIDGE_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})


def _open_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    # mode=ro enforces no writes while still allowing SQLite to read a live
    # database's WAL. immutable=1 would silently omit uncheckpointed calls.
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {
        "sessions": {"id", "source", "started_at", "ended_at"},
        "messages": {
            "id",
            "session_id",
            "role",
            "tool_call_id",
            "tool_calls",
            "tool_name",
            "timestamp",
        },
    }
    for table, columns in required.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        actual = {row["name"] for row in rows}
        missing = columns - actual
        if missing:
            raise ValueError(
                f"Unsupported state DB: {table} is missing columns: "
                f"{', '.join(sorted(missing))}"
            )


def _tool_calls_from_json(raw: Any) -> Iterable[tuple[str | None, str]]:
    if not raw:
        return ()
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(values, list):
        return ()

    calls: list[tuple[str | None, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        function = value.get("function")
        name = function.get("name") if isinstance(function, dict) else value.get("name")
        call_id = value.get("id") or value.get("tool_call_id")
        if name:
            calls.append((str(call_id) if call_id else None, str(name)))
    return calls


def load_sessions_and_calls(
    db_path: Path, session_ids: Iterable[str] = ()
) -> tuple[dict[str, dict[str, Any]], list[PersistedCall], int]:
    """Read sessions and unique tool calls without conflating sessions.

    Assistant tool_calls are preferred because they represent invocation time.
    Tool-result rows fill gaps for older/incomplete transcripts. Repeated rows
    with the same ``(session_id, tool_call_id)`` are counted once. ID-less
    legacy calls cannot be correlated safely, so each is retained using its
    stable message-row identity (and array index for assistant rows).
    """
    selected = tuple(dict.fromkeys(session_ids))
    with closing(_open_read_only(db_path)) as connection:
        _require_schema(connection)
        where = ""
        params: tuple[str, ...] = ()
        if selected:
            placeholders = ",".join("?" for _ in selected)
            where = f" WHERE id IN ({placeholders})"
            params = selected
        session_rows = connection.execute(
            "SELECT id, source, started_at, ended_at FROM sessions" + where,
            params,
        ).fetchall()
        sessions = {
            row["id"]: {
                "session_id": row["id"],
                "source": row["source"],
                "started_at": float(row["started_at"]),
                "ended_at": float(row["ended_at"])
                if row["ended_at"] is not None
                else None,
            }
            for row in session_rows
        }
        if not sessions:
            return sessions, [], 0

        placeholders = ",".join("?" for _ in sessions)
        message_rows = connection.execute(
            "SELECT id, session_id, role, tool_call_id, tool_calls, tool_name, timestamp "
            f"FROM messages WHERE session_id IN ({placeholders}) "
            "AND (tool_calls IS NOT NULL OR tool_call_id IS NOT NULL OR tool_name IS NOT NULL) "
            "ORDER BY timestamp, id",
            tuple(sessions),
        ).fetchall()

    by_identity: dict[tuple[str, str, str], PersistedCall] = {}
    persisted_rows = 0
    # First pass: invocations. This preserves first-use timing even if results arrive later.
    for row in message_rows:
        for index, (call_id, name) in enumerate(_tool_calls_from_json(row["tool_calls"])):
            persisted_rows += 1
            identity = (
                (row["session_id"], "id", call_id)
                if call_id is not None
                else (row["session_id"], "assistant-row", f"{row['id']}:{index}")
            )
            by_identity.setdefault(
                identity,
                PersistedCall(
                    row["session_id"], call_id, name, float(row["timestamp"]),
                    f"message:{row['id']}:{index}",
                ),
            )
    # Second pass: tool results absent from assistant JSON (legacy/interrupted records).
    for row in message_rows:
        call_id = row["tool_call_id"]
        name = row["tool_name"]
        if name:
            persisted_rows += 1
            normalized_id = str(call_id) if call_id else None
            identity = (
                (row["session_id"], "id", normalized_id)
                if normalized_id is not None
                else (row["session_id"], "tool-row", str(row["id"]))
            )
            by_identity.setdefault(
                identity,
                PersistedCall(
                    row["session_id"], normalized_id, str(name),
                    float(row["timestamp"]), f"message:{row['id']}",
                ),
            )
    calls = sorted(
        by_identity.values(),
        key=lambda call: (call.timestamp, call.session_id, call.row_identity),
    )
    return sessions, calls, persisted_rows


def _round(value: float) -> float:
    return round(value, 6)


def build_workload_report(
    sessions: dict[str, dict[str, Any]], calls: list[PersistedCall], persisted_rows: int
) -> dict[str, Any]:
    calls_by_session: dict[str, list[PersistedCall]] = defaultdict(list)
    for call in calls:
        calls_by_session[call.session_id].append(call)

    session_reports: list[dict[str, Any]] = []
    global_tools: Counter[str] = Counter()
    global_pairs: Counter[tuple[str, str]] = Counter()
    for session_id in sorted(
        sessions, key=lambda key: (sessions[key]["started_at"], key)
    ):
        metadata = sessions[session_id]
        session_calls = calls_by_session.get(session_id, [])
        counts = Counter(call.tool_name for call in session_calls)
        global_tools.update(counts)
        names = sorted(counts)
        pairs = list(itertools.combinations(names, 2))
        global_pairs.update(pairs)
        first_by_tool: dict[str, float] = {}
        for call in session_calls:
            first_by_tool.setdefault(call.tool_name, call.timestamp)

        total = len(session_calls)
        tool_stats = [
            {
                "name": name,
                "call_count": counts[name],
                "call_fraction": _round(counts[name] / total) if total else 0.0,
                "first_use_seconds": _round(
                    max(0.0, first_by_tool[name] - metadata["started_at"])
                ),
            }
            for name in names
        ]
        session_reports.append({
            **metadata,
            "tool_call_count": total,
            "tool_names": names,
            "tools": tool_stats,
            "cooccurrence": [{"tools": [left, right]} for left, right in pairs],
        })

    total_calls = len(calls)
    duplicate_rows = max(0, persisted_rows - total_calls)
    return {
        "summary": {
            "session_count": len(sessions),
            "sessions_with_tools": sum(
                bool(item["tool_call_count"]) for item in session_reports
            ),
            "persisted_tool_call_rows": persisted_rows,
            "unique_tool_call_count": total_calls,
            "deduplicated_row_count": duplicate_rows,
            "unique_tool_count": len(global_tools),
        },
        "tools": [
            {
                "name": name,
                "call_count": count,
                "call_fraction": _round(count / total_calls) if total_calls else 0.0,
                "session_count": sum(
                    name in item["tool_names"] for item in session_reports
                ),
            }
            for name, count in sorted(
                global_tools.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "cooccurrence": [
            {"tools": [left, right], "session_count": count}
            for (left, right), count in sorted(
                global_pairs.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "sessions": session_reports,
    }


def parse_named_csv(values: Iterable[str], option: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for value in values:
        name, separator, raw_items = value.partition("=")
        items = {item.strip() for item in raw_items.split(",") if item.strip()}
        if not separator or not name.strip() or not items:
            raise ValueError(f"{option} expects NAME=item1,item2")
        result[name.strip()].update(items)
    return dict(result)


def _load_registry() -> tuple[Any, float]:
    started = time.perf_counter()
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    return registry, (time.perf_counter() - started) * 1000.0


def resolve_candidates(
    candidate_tools: dict[str, set[str]], candidate_toolsets: dict[str, set[str]]
) -> tuple[dict[str, set[str]], dict[str, list[str]], float | None]:
    resolved = {name: set(tools) for name, tools in candidate_tools.items()}
    unknown: dict[str, list[str]] = {}
    discovery_ms: float | None = None
    if candidate_toolsets:
        _registry, discovery_ms = _load_registry()
        from toolsets import resolve_toolset, validate_toolset

        for candidate, toolsets in candidate_toolsets.items():
            missing = sorted(
                toolset for toolset in toolsets if not validate_toolset(toolset)
            )
            if missing:
                unknown[candidate] = missing
            for toolset in toolsets - set(missing):
                resolved.setdefault(candidate, set()).update(resolve_toolset(toolset))
    for name in candidate_toolsets:
        resolved.setdefault(name, set())
    return resolved, unknown, discovery_ms


def compare_candidates(
    report: dict[str, Any],
    candidates: dict[str, set[str]],
    unknown: dict[str, list[str]],
    candidate_toolsets: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """Compare persisted workload with explicit uncertainty semantics.

    Direct allowlists are exact for ordinary persisted tool names. Toolset
    candidates are necessarily estimates: this static resolver does not run
    availability filtering or progressive-disclosure assembly. Deferred bridge
    rows also hide the underlying tool name because this privacy-preserving
    audit deliberately does not read tool arguments. In either case exact
    impact fields are null and conservative bounds are emitted instead.
    """
    candidate_toolsets = candidate_toolsets or {}
    actual_counts = {item["name"]: item["call_count"] for item in report["tools"]}
    actual_names = set(actual_counts)
    total_calls = report["summary"]["unique_tool_call_count"]
    total_sessions = report["summary"]["session_count"]
    sessions_with_tools = [
        item for item in report["sessions"] if item["tool_call_count"]
    ]
    tool_session_denominator = len(sessions_with_tools)
    deferred_names = actual_names & DEFERRED_BRIDGE_NAMES
    results = []
    for name in sorted(candidates):
        allowed = candidates[name]
        missing_names = sorted(actual_names - allowed)
        missing_calls = sum(actual_counts[tool] for tool in missing_names)
        affected_sessions = {
            item["session_id"]
            for item in sessions_with_tools
            if set(item["tool_names"]) - allowed
        }
        affected = len(affected_sessions)

        reasons = []
        unknown_toolsets = unknown.get(name, [])
        if unknown_toolsets:
            reasons.append("unknown_toolsets_may_add_tools")
        if name in candidate_toolsets:
            reasons.append("runtime_tool_surface_not_simulated")
        if deferred_names:
            reasons.append("deferred_bridge_workload_not_expanded")
        complete = not reasons

        may_add_tools = bool(unknown_toolsets)
        runtime_dynamic = name in candidate_toolsets
        definitely_missing_names = (
            []
            if may_add_tools
            else sorted((actual_names - allowed) - DEFERRED_BRIDGE_NAMES)
        )
        lower_missing_calls = sum(actual_counts[tool] for tool in definitely_missing_names)
        lower_affected_sessions = (
            0
            if may_add_tools
            else sum(
                bool((set(item["tool_names"]) - allowed) - DEFERRED_BRIDGE_NAMES)
                for item in sessions_with_tools
            )
        )
        if runtime_dynamic:
            observed_upper_missing_tools = len(actual_names)
            observed_upper_missing_calls = total_calls
            observed_upper_affected = tool_session_denominator
        else:
            observed_upper_missing_tools = len(missing_names)
            observed_upper_missing_calls = missing_calls
            deferred_sessions = {
                item["session_id"]
                for item in sessions_with_tools
                if set(item["tool_names"]) & deferred_names
            }
            observed_upper_affected = len(affected_sessions | deferred_sessions)

        # A deferred row may stand for underlying workload absent from this
        # name-only audit, so authoritative count upper bounds are unbounded.
        count_upper = None if deferred_names else observed_upper_missing_calls
        tool_count_upper = None if deferred_names else observed_upper_missing_tools

        def rate(numerator: int, denominator: int) -> float:
            return _round(numerator / denominator) if denominator else 0.0

        exact = {
            "missing_tools": missing_names,
            "missing_tool_count": len(missing_names),
            "missing_tool_rate": rate(len(missing_names), len(actual_names)),
            "missing_call_count": missing_calls,
            "missing_call_rate": rate(missing_calls, total_calls),
            "affected_session_count": affected,
            "affected_tool_session_rate": rate(affected, tool_session_denominator),
            "affected_all_session_rate": rate(affected, total_sessions),
        }
        if not complete:
            exact = {key: None for key in exact}

        results.append({
            "name": name,
            "allowed_tools": sorted(allowed),
            "unknown_toolsets": unknown_toolsets,
            "impact_complete": complete,
            "impact_incompleteness_reasons": reasons,
            "impact_scope": "persisted tool names; deferred bridge payloads are not read",
            "observed_deferred_bridge_tools": sorted(deferred_names),
            "affected_tool_session_denominator": tool_session_denominator,
            "affected_all_session_denominator": total_sessions,
            **exact,
            "definitely_missing_tools": definitely_missing_names,
            "impact_bounds": {
                "missing_tool_count": {
                    "lower": len(definitely_missing_names),
                    "upper": tool_count_upper,
                },
                "missing_call_count": {
                    "lower": lower_missing_calls,
                    "upper": count_upper,
                },
                "missing_call_rate": {
                    "lower": 0.0 if deferred_names else rate(lower_missing_calls, total_calls),
                    "upper": 1.0 if deferred_names else rate(observed_upper_missing_calls, total_calls),
                },
                "affected_session_count": {
                    "lower": lower_affected_sessions,
                    "upper": observed_upper_affected,
                },
                "affected_tool_session_rate": {
                    "lower": rate(lower_affected_sessions, tool_session_denominator),
                    "upper": rate(observed_upper_affected, tool_session_denominator),
                },
                "affected_all_session_rate": {
                    "lower": rate(lower_affected_sessions, total_sessions),
                    "upper": rate(observed_upper_affected, total_sessions),
                },
            },
        })
    return results


def _callable_name(value: Any) -> str:
    """Return a deterministic callable label without address-bearing repr()."""
    module = getattr(value, "__module__", None) or type(value).__module__
    qualname = getattr(value, "__qualname__", None) or type(value).__qualname__
    return f"{module}.{qualname}"


def measure_availability(include_diagnostics: bool = False) -> dict[str, Any]:
    """Invoke each unique registered check_fn once.

    This deliberately bypasses the registry TTL cache so the result measures the
    actual existing check. It does not save config or execute tool handlers.
    Timings are excluded by default for deterministic JSON and are placed under
    diagnostics only when explicitly requested.
    """
    registry, discovery_ms = _load_registry()
    grouped: dict[Any, dict[str, set[str]]] = {}
    for tool_name in registry.get_all_tool_names():
        entry = registry.get_entry(tool_name)
        if entry is None or entry.check_fn is None:
            continue
        details = grouped.setdefault(
            entry.check_fn, {"tools": set(), "toolsets": set()}
        )
        details["tools"].add(entry.name)
        details["toolsets"].add(entry.toolset)

    checks = []
    for check_fn, details in sorted(
        grouped.items(),
        key=lambda item: (_callable_name(item[0]), sorted(item[1]["tools"])),
    ):
        started = time.perf_counter()
        error_type = None
        try:
            available = bool(check_fn())
        except Exception as exc:  # existing checks are allowed to fail closed
            available = False
            error_type = type(exc).__name__
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        check = {
            "check": _callable_name(check_fn),
            "tools": sorted(details["tools"]),
            "toolsets": sorted(details["toolsets"]),
            "available": available,
            "error_type": error_type,
        }
        if include_diagnostics:
            check["elapsed_ms"] = round(elapsed_ms, 3)
        checks.append(check)
    result = {
        "explicitly_invoked": True,
        "check_count": len(checks),
        "checks": checks,
    }
    if include_diagnostics:
        result["diagnostics"] = {
            "registry_discovery_ms": round(discovery_ms, 3),
            "total_check_ms": round(sum(item["elapsed_ms"] for item in checks), 3),
        }
    return result


def render_human(report: dict[str, Any], session_limit: int = 20) -> str:
    summary = report["summary"]
    lines = [
        "Hermes tool workload audit (read-only)",
        (
            f"Sessions {summary['session_count']} | with tools {summary['sessions_with_tools']} | "
            f"unique calls {summary['unique_tool_call_count']} | "
            f"deduplicated rows {summary['deduplicated_row_count']}"
        ),
        "",
        f"{'Session':<18} {'Calls':>6}  Tools",
        f"{'-' * 18} {'-' * 6}  {'-' * 44}",
    ]
    ranked_sessions = sorted(
        report["sessions"],
        key=lambda item: (-item["tool_call_count"], item["session_id"]),
    )
    displayed_sessions = ranked_sessions[:session_limit]
    for session in displayed_sessions:
        session_id = session["session_id"]
        short_id = session_id if len(session_id) <= 18 else session_id[:15] + "..."
        tools = ", ".join(session["tool_names"]) or "-"
        if len(tools) > 80:
            tools = tools[:77] + "..."
        lines.append(f"{short_id:<18} {session['tool_call_count']:>6}  {tools}")
    omitted = len(ranked_sessions) - len(displayed_sessions)
    if omitted:
        lines.append(f"... {omitted} more sessions in JSON")

    if report.get("candidates"):
        lines.extend([
            "",
            f"{'Candidate':<18} {'Missing calls':>13} {'Rate':>8} {'Affected sessions':>18}",
            f"{'-' * 18} {'-' * 13} {'-' * 8} {'-' * 18}",
        ])
        for candidate in report["candidates"]:
            if candidate["impact_complete"]:
                missing = str(candidate["missing_call_count"])
                rate_text = f"{candidate['missing_call_rate']:.1%}"
                affected = str(candidate["affected_session_count"])
            else:
                bounds = candidate["impact_bounds"]
                upper = bounds["missing_call_count"]["upper"]
                missing = f">={bounds['missing_call_count']['lower']}"
                if upper is not None:
                    missing += f"..<={upper}"
                rate_text = "incomplete"
                affected = "bounded"
            lines.append(
                f"{candidate['name']:<18} {missing:>13} "
                f"{rate_text:>8} {affected:>18}"
            )

    availability = report.get("availability")
    if availability and availability.get("explicitly_invoked"):
        line = f"Availability checks: {availability['check_count']} unique checks"
        diagnostics = availability.get("diagnostics")
        if diagnostics:
            line += (
                f", {diagnostics['total_check_ms']:.3f} ms total "
                f"(+ {diagnostics['registry_discovery_ms']:.3f} ms registry discovery)"
            )
        lines.extend(["", line])
    return "\n".join(lines)


def _same_file(left: Path, right: Path) -> bool:
    """Compare existing paths by identity, following symlink/hardlink aliases."""
    try:
        return os.path.samefile(left.expanduser(), right.expanduser())
    except FileNotFoundError:
        return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _reject_db_output_alias(db_path: Path, output_path: Path | None) -> None:
    if output_path is not None and _same_file(db_path, output_path):
        raise ValueError("--json-output must not be the selected state DB or an alias of it")


def _write_json_safely(output_path: Path, db_path: Path, payload: str) -> None:
    """Atomically replace output without ever opening a DB alias for writing."""
    output = output_path.expanduser()
    _reject_db_output_alias(db_path, output)
    seed_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent,
    )
    os.close(seed_descriptor)
    temporary = Path(temporary_name)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_TRUNC)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(payload)
        # Recheck immediately before replacement. If output was raced to a DB
        # alias, fail closed; replacing it would not corrupt the DB, but should
        # still honor the CLI's alias rejection contract.
        _reject_db_output_alias(db_path, output)
        os.replace(temporary, output)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, required=True, help="Profile state.db to open read-only"
    )
    parser.add_argument(
        "--session-id",
        action="append",
        default=[],
        help="Restrict to a session ID (repeatable)",
    )
    parser.add_argument(
        "--candidate-tools",
        action="append",
        default=[],
        metavar="NAME=TOOL,...",
        help="Candidate direct tool allowlist (repeatable; same names merge)",
    )
    parser.add_argument(
        "--candidate-toolsets",
        action="append",
        default=[],
        metavar="NAME=TOOLSET,...",
        help="Candidate registered-toolset allowlist (repeatable; same names merge)",
    )
    parser.add_argument(
        "--check-availability",
        action="store_true",
        help="Explicitly invoke each unique registered tool availability check",
    )
    parser.add_argument(
        "--include-diagnostics",
        action="store_true",
        help="Include nondeterministic discovery and availability timings in JSON",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="Write JSON to this path; default writes JSON to stdout and the table to stderr",
    )
    parser.add_argument(
        "--human-session-limit",
        type=int,
        default=20,
        help="Maximum highest-frequency sessions in the human table (default: 20)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _reject_db_output_alias(args.db, args.json_output)
        tools = parse_named_csv(args.candidate_tools, "--candidate-tools")
        toolsets = parse_named_csv(args.candidate_toolsets, "--candidate-toolsets")
        sessions, calls, persisted_rows = load_sessions_and_calls(
            args.db, args.session_id
        )
        report = build_workload_report(sessions, calls, persisted_rows)
        candidates, unknown, discovery_ms = resolve_candidates(tools, toolsets)
        report["candidates"] = compare_candidates(report, candidates, unknown, toolsets)
        if args.include_diagnostics and discovery_ms is not None:
            report.setdefault("diagnostics", {})[
                "candidate_registry_discovery_ms"
            ] = round(discovery_ms, 3)
        report["availability"] = (
            measure_availability(args.include_diagnostics)
            if args.check_availability
            else {"explicitly_invoked": False}
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.human_session_limit < 0:
        print("error: --human-session-limit must be non-negative", file=sys.stderr)
        return 2
    human = render_human(report, args.human_session_limit)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output is None:
        print(human, file=sys.stderr)
        sys.stdout.write(payload)
    else:
        try:
            _write_json_safely(args.json_output, args.db, payload)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(human)
        print(f"JSON: {args.json_output.expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
