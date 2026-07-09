"""Read-only Claude Code session telemetry helpers for Mini App dashboards."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

MAX_TIMELINE_EVENTS = 100
MAX_LARGEST_TOOL_RESULTS = 8
MAX_LIST_SESSIONS = 50


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _iso_mtime(path: Path) -> Optional[str]:
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _preview(value: Any, limit: int = 220) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            text = str(value)
    text = text.replace("\x00", "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _counter_dict(counter: Counter) -> Dict[str, int]:
    return {str(k): int(v) for k, v in counter.items() if k is not None}


def _claude_projects_dir(claude_home: Optional[Path] = None) -> Path:
    return (claude_home or Path.home() / ".claude") / "projects"


def iter_claude_transcripts(claude_home: Optional[Path] = None) -> Iterable[Path]:
    projects = _claude_projects_dir(claude_home)
    if not projects.exists():
        return []
    try:
        return sorted(
            (p for p in projects.glob("**/*.jsonl") if "/subagents/" not in str(p)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return []


def session_id_from_transcript(path: Path) -> str:
    return path.stem


def _project_slug_for(path: Path, claude_home: Optional[Path] = None) -> str:
    try:
        rel = path.relative_to(_claude_projects_dir(claude_home))
        return rel.parts[0] if rel.parts else ""
    except Exception:
        return path.parent.name


def _subagent_count(path: Path) -> int:
    subagents = path.with_suffix("") / "subagents"
    if not subagents.exists():
        return 0
    try:
        return sum(1 for p in subagents.glob("*.jsonl") if p.is_file())
    except Exception:
        return 0


def summarize_claude_transcript(path: Path, *, include_events: bool = True, claude_home: Optional[Path] = None) -> Dict[str, Any]:
    warnings: List[str] = []
    record_counts: Counter = Counter()
    system_subtypes: Counter = Counter()
    model_counts: Counter = Counter()
    stop_reason_counts: Counter = Counter()
    tool_call_counts: Counter = Counter()
    usage = Counter()
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    timeline: List[Dict[str, Any]] = []
    largest_tool_results: List[Dict[str, Any]] = []
    tool_result_bytes = 0
    malformed_lines = 0

    try:
        stat = path.stat()
    except Exception as exc:
        return {
            "ok": False,
            "session_id": session_id_from_transcript(path),
            "transcript_path": str(path),
            "error": str(exc),
            "warnings": ["transcript_unreadable"],
        }

    def add_event(event: Dict[str, Any]) -> None:
        if include_events:
            timeline.append(event)
            if len(timeline) > MAX_TIMELINE_EVENTS * 2:
                del timeline[: len(timeline) - MAX_TIMELINE_EVENTS]

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    malformed_lines += 1
                    continue

                ts = obj.get("timestamp")
                if isinstance(ts, str) and ts:
                    first_ts = first_ts or ts
                    last_ts = ts

                typ = obj.get("type") or obj.get("role") or "unknown"
                record_counts[str(typ)] += 1
                subtype = obj.get("subtype")
                if typ == "system" and subtype:
                    system_subtypes[str(subtype)] += 1

                if typ == "user" and obj.get("promptId"):
                    add_event({"kind": "user_prompt", "timestamp": ts, "line": line_no, "uuid": obj.get("uuid"), "preview": _preview((obj.get("message") or {}).get("content") if isinstance(obj.get("message"), dict) else obj.get("content"))})
                elif typ == "attachment":
                    add_event({"kind": "attachment", "timestamp": ts, "line": line_no, "uuid": obj.get("uuid"), "preview": _preview(obj.get("attachment"))})
                elif typ == "queue-operation":
                    add_event({"kind": "queue_operation", "timestamp": ts, "line": line_no, "uuid": obj.get("uuid"), "operation": obj.get("operation")})
                elif typ == "system" and subtype:
                    add_event({"kind": str(subtype), "timestamp": ts, "line": line_no, "uuid": obj.get("uuid"), "preview": _preview(obj.get("compact_metadata") or obj.get("content") or obj)})

                message = obj.get("message")
                if isinstance(message, dict):
                    model = message.get("model")
                    if model:
                        model_counts[str(model)] += 1
                    stop_reason = message.get("stop_reason")
                    if stop_reason:
                        stop_reason_counts[str(stop_reason)] += 1
                    msg_usage = message.get("usage") or {}
                    if isinstance(msg_usage, dict):
                        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                            usage[key] += _safe_int(msg_usage.get(key))
                        server_tool_use = msg_usage.get("server_tool_use") or {}
                        if isinstance(server_tool_use, dict):
                            usage["web_search_requests"] += _safe_int(server_tool_use.get("web_search_requests"))
                            usage["web_fetch_requests"] += _safe_int(server_tool_use.get("web_fetch_requests"))
                    content = message.get("content")
                    tool_names: List[str] = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                name = str(block.get("name") or "unknown")
                                tool_call_counts[name] += 1
                                tool_names.append(name)
                                add_event({
                                    "kind": "tool_call",
                                    "timestamp": ts,
                                    "line": line_no,
                                    "uuid": block.get("id") or obj.get("uuid"),
                                    "tool_name": name,
                                    "preview": _preview(block.get("input")),
                                })
                    if typ == "assistant":
                        add_event({
                            "kind": "assistant_turn",
                            "timestamp": ts,
                            "line": line_no,
                            "uuid": obj.get("uuid"),
                            "stop_reason": stop_reason,
                            "model": model,
                            "tool_names": tool_names,
                            "usage": {k: _safe_int(msg_usage.get(k)) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")} if isinstance(msg_usage, dict) else {},
                        })

                tool_result = obj.get("toolUseResult")
                if tool_result is not None:
                    size = len(_preview(tool_result, limit=20_000_000).encode("utf-8", errors="replace"))
                    tool_result_bytes += size
                    item = {
                        "bytes": size,
                        "timestamp": ts,
                        "line": line_no,
                        "uuid": obj.get("uuid"),
                        "source_tool_assistant_uuid": obj.get("sourceToolAssistantUUID"),
                        "preview": _preview(tool_result),
                    }
                    largest_tool_results.append(item)
                    largest_tool_results.sort(key=lambda x: x.get("bytes", 0), reverse=True)
                    del largest_tool_results[MAX_LARGEST_TOOL_RESULTS:]
                    add_event({"kind": "tool_result", **item})
    except Exception as exc:
        warnings.append(f"read_error:{exc}")

    if malformed_lines:
        warnings.append(f"malformed_jsonl_lines:{malformed_lines}")

    compact_count = system_subtypes.get("compact_boundary", 0)
    token_total = sum(_safe_int(usage.get(k)) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if include_events:
        timeline = timeline[-MAX_TIMELINE_EVENTS:]

    return {
        "ok": True,
        "session_id": session_id_from_transcript(path),
        "project_slug": _project_slug_for(path, claude_home),
        "transcript_path": str(path),
        "mtime": _iso_mtime(path),
        "size_bytes": int(stat.st_size),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "record_counts": _counter_dict(record_counts),
        "system_subtypes": _counter_dict(system_subtypes),
        "prompt_count": int(record_counts.get("user", 0)),
        "assistant_turn_count": int(record_counts.get("assistant", 0)),
        "attachment_count": int(record_counts.get("attachment", 0)),
        "compact_count": int(compact_count),
        "subagent_count": _subagent_count(path),
        "model_counts": _counter_dict(model_counts),
        "stop_reason_counts": _counter_dict(stop_reason_counts),
        "usage": {**_counter_dict(usage), "total_context_related_tokens": int(token_total)},
        "tool_call_counts": _counter_dict(tool_call_counts),
        "tool_result_bytes": int(tool_result_bytes),
        "largest_tool_results": largest_tool_results,
        "timeline": timeline if include_events else [],
        "warnings": warnings,
    }


def list_claude_sessions(*, claude_home: Optional[Path] = None, limit: int = MAX_LIST_SESSIONS) -> Dict[str, Any]:
    warnings: List[str] = []
    projects = _claude_projects_dir(claude_home)
    if not projects.exists():
        return {"ok": True, "source": "claude_transcripts", "claude_projects_dir": str(projects), "sessions": [], "warnings": ["claude_projects_missing"]}
    sessions: List[Dict[str, Any]] = []
    for path in list(iter_claude_transcripts(claude_home))[: max(1, min(limit, 200))]:
        summary = summarize_claude_transcript(path, include_events=False, claude_home=claude_home)
        sessions.append(summary)
    return {"ok": True, "source": "claude_transcripts", "claude_projects_dir": str(projects), "sessions": sessions, "warnings": warnings}


def find_claude_transcript(session_id: str, *, claude_home: Optional[Path] = None) -> Optional[Path]:
    safe_id = os.path.basename(session_id.strip())
    if not safe_id or safe_id in {".", ".."}:
        return None
    for path in iter_claude_transcripts(claude_home):
        if path.stem == safe_id:
            return path
    return None


def get_claude_session(session_id: str, *, claude_home: Optional[Path] = None, include_events: bool = True) -> Dict[str, Any]:
    path = find_claude_transcript(session_id, claude_home=claude_home)
    if path is None:
        return {"ok": False, "session_id": session_id, "error": "Claude session transcript not found", "warnings": ["session_not_found"]}
    return summarize_claude_transcript(path, include_events=include_events, claude_home=claude_home)
