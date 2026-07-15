from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.tool_workload_audit import (
    build_workload_report,
    compare_candidates,
    load_sessions_and_calls,
    main,
)


def _state_db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL
        );
        """
    )
    connection.executemany(
        "INSERT INTO sessions(id, source, started_at, ended_at) VALUES (?, ?, ?, ?)",
        [
            ("session-one", "cli", 100.0, 160.0),
            ("session-two", "telegram", 200.0, 260.0),
        ],
    )

    terminal_call = json.dumps([
        {"id": "call-a", "type": "function", "function": {"name": "terminal"}}
    ])
    read_call = json.dumps([
        {"id": "call-b", "type": "function", "function": {"name": "read_file"}}
    ])
    second_terminal_call = json.dumps([
        {"id": "call-c", "type": "function", "function": {"name": "terminal"}}
    ])
    connection.executemany(
        """
        INSERT INTO messages(session_id, role, tool_call_id, tool_calls, tool_name, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("session-one", "assistant", None, terminal_call, None, 105.0),
            # Re-persisted assistant invocation: same ID must not increase counts.
            ("session-one", "assistant", None, terminal_call, None, 106.0),
            # Result row repeats the same persisted call ID and must not count again.
            ("session-one", "tool", "call-a", None, "terminal", 110.0),
            ("session-one", "assistant", None, read_call, None, 120.0),
            ("session-one", "tool", "call-b", None, "read_file", 125.0),
            ("session-two", "assistant", None, second_terminal_call, None, 203.0),
            ("session-two", "tool", "call-c", None, "terminal", 204.0),
        ],
    )
    connection.commit()
    connection.close()
    return path


def test_deduplicates_calls_and_reports_session_workload(tmp_path: Path) -> None:
    db_path = _state_db(tmp_path)

    sessions, calls, persisted_rows = load_sessions_and_calls(db_path)
    report = build_workload_report(sessions, calls, persisted_rows)

    assert [call.tool_call_id for call in calls] == ["call-a", "call-b", "call-c"]
    assert report["summary"] == {
        "session_count": 2,
        "sessions_with_tools": 2,
        "persisted_tool_call_rows": 7,
        "unique_tool_call_count": 3,
        "deduplicated_row_count": 4,
        "unique_tool_count": 2,
    }

    first = report["sessions"][0]
    assert first["tool_call_count"] == 2
    assert first["tool_names"] == ["read_file", "terminal"]
    assert first["cooccurrence"] == [{"tools": ["read_file", "terminal"]}]
    assert first["tools"] == [
        {
            "name": "read_file",
            "call_count": 1,
            "call_fraction": 0.5,
            "first_use_seconds": 20.0,
        },
        {
            "name": "terminal",
            "call_count": 1,
            "call_fraction": 0.5,
            "first_use_seconds": 5.0,
        },
    ]
    assert report["tools"][0]["name"] == "terminal"
    assert report["tools"][0]["call_count"] == 2


def test_candidate_missing_rates_use_deduplicated_calls(tmp_path: Path) -> None:
    db_path = _state_db(tmp_path)
    sessions, calls, persisted_rows = load_sessions_and_calls(db_path)
    report = build_workload_report(sessions, calls, persisted_rows)

    candidates = compare_candidates(report, {"terminal-only": {"terminal"}}, {})

    assert candidates == [
        {
            "name": "terminal-only",
            "allowed_tools": ["terminal"],
            "unknown_toolsets": [],
            "missing_tools": ["read_file"],
            "missing_tool_count": 1,
            "missing_tool_rate": 0.5,
            "missing_call_count": 1,
            "missing_call_rate": 0.333333,
            "affected_session_count": 1,
            "affected_session_rate": 0.5,
        }
    ]


def test_cli_emits_json_without_message_content(tmp_path: Path, capsys) -> None:
    db_path = _state_db(tmp_path)
    output = tmp_path / "audit.json"

    result = main([
        "--db",
        str(db_path),
        "--candidate-tools",
        "terminal-only=terminal",
        "--json-output",
        str(output),
    ])

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["availability"] == {"explicitly_invoked": False}
    assert payload["candidates"][0]["missing_call_rate"] == 0.333333
    assert "Hermes tool workload audit (read-only)" in capsys.readouterr().out
    serialized = output.read_text(encoding="utf-8")
    assert "content" not in serialized
    assert "arguments" not in serialized
