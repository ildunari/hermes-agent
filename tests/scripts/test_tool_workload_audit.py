from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import scripts.tool_workload_audit as audit
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
            content TEXT,
            timestamp REAL NOT NULL
        );
        """
    )
    connection.executemany(
        "INSERT INTO sessions(id, source, started_at, ended_at) VALUES (?, ?, ?, ?)",
        [
            ("session-one", "cli", 100.0, 160.0),
            ("session-two", "telegram", 200.0, 260.0),
            ("session-empty", "cli", 300.0, 360.0),
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
        "session_count": 3,
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

    candidate = candidates[0]
    assert candidate["impact_complete"] is True
    assert candidate["missing_tools"] == ["read_file"]
    assert candidate["missing_call_count"] == 1
    assert candidate["missing_call_rate"] == 0.333333
    assert candidate["affected_session_count"] == 1
    assert candidate["affected_tool_session_denominator"] == 2
    assert candidate["affected_tool_session_rate"] == 0.5
    assert candidate["affected_all_session_denominator"] == 3
    assert candidate["affected_all_session_rate"] == 0.333333
    assert "affected_session_rate" not in candidate


def test_ids_are_session_scoped_and_idless_legacy_rows_remain_distinct(tmp_path: Path) -> None:
    db_path = _state_db(tmp_path)
    connection = sqlite3.connect(db_path)
    shared = json.dumps([
        {"id": "shared", "function": {"name": "terminal"}},
        {"function": {"name": "write_file"}},
    ])
    connection.executemany(
        """
        INSERT INTO messages(session_id, role, tool_call_id, tool_calls, tool_name, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("session-one", "assistant", None, shared, None, 130.0),
            ("session-two", "assistant", None, shared, None, 230.0),
            ("session-one", "tool", None, None, "legacy_one", 140.0),
            ("session-one", "tool", None, None, "legacy_two", 141.0),
        ],
    )
    connection.commit()
    connection.close()

    _sessions, calls, _rows = load_sessions_and_calls(db_path)
    shared_calls = [call for call in calls if call.tool_call_id == "shared"]
    assert {call.session_id for call in shared_calls} == {"session-one", "session-two"}
    assert [call.tool_name for call in calls].count("write_file") == 2
    assert {call.tool_name for call in calls if call.tool_call_id is None} >= {
        "legacy_one", "legacy_two"
    }
    assert len({call.row_identity for call in calls if call.tool_call_id is None}) == 4


def test_unknown_toolsets_and_deferred_bridges_null_exact_impact(tmp_path: Path) -> None:
    db_path = _state_db(tmp_path)
    connection = sqlite3.connect(db_path)
    bridge = json.dumps([
        {"id": "bridge", "function": {"name": "tool_call", "arguments": "SECRET"}}
    ])
    connection.execute(
        "INSERT INTO messages(session_id, role, tool_calls, timestamp) VALUES (?, ?, ?, ?)",
        ("session-one", "assistant", bridge, 150.0),
    )
    connection.commit()
    connection.close()
    sessions, calls, rows = load_sessions_and_calls(db_path)
    report = build_workload_report(sessions, calls, rows)

    candidate = compare_candidates(
        report,
        {"uncertain": {"terminal"}},
        {"uncertain": ["unknown-plugin"]},
        {"uncertain": {"terminal", "unknown-plugin"}},
    )[0]

    assert candidate["impact_complete"] is False
    for field in (
        "missing_tools",
        "missing_tool_count",
        "missing_tool_rate",
        "missing_call_count",
        "missing_call_rate",
        "affected_session_count",
        "affected_tool_session_rate",
        "affected_all_session_rate",
    ):
        assert candidate[field] is None
    assert candidate["observed_deferred_bridge_tools"] == ["tool_call"]
    assert set(candidate["impact_incompleteness_reasons"]) == {
        "unknown_toolsets_may_add_tools",
        "runtime_tool_surface_not_simulated",
        "deferred_bridge_workload_not_expanded",
    }
    assert candidate["impact_bounds"]["missing_call_count"] == {
        "lower": 0,
        "upper": None,
    }


def test_cli_emits_json_without_message_content(tmp_path: Path, capsys) -> None:
    db_path = _state_db(tmp_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE messages SET content = ? WHERE id = 1", ("TOP-SECRET-PROMPT",)
    )
    connection.commit()
    connection.close()
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
    assert "TOP-SECRET-PROMPT" not in serialized


def test_cli_overwrites_longer_json_output_without_trailing_bytes(
    tmp_path: Path, capsys,
) -> None:
    db_path = _state_db(tmp_path)
    output = tmp_path / "audit.json"
    output.write_text('{"obsolete": "' + ("x" * 4096) + '"}', encoding="utf-8")

    assert main(["--db", str(db_path), "--json-output", str(output)]) == 0

    serialized = output.read_text(encoding="utf-8")
    payload = json.loads(serialized)
    assert payload["summary"]["unique_tool_call_count"] == 3
    assert "obsolete" not in serialized
    assert len(serialized) < 4096
    capsys.readouterr()


@pytest.mark.parametrize("alias_kind", ["same", "symlink", "hardlink"])
def test_cli_rejects_db_output_aliases_without_modifying_db(
    tmp_path: Path, capsys, alias_kind: str
) -> None:
    db_path = _state_db(tmp_path)
    before = db_path.read_bytes()
    if alias_kind == "same":
        output = db_path
    elif alias_kind == "symlink":
        output = tmp_path / "state-link.db"
        output.symlink_to(db_path)
    else:
        output = tmp_path / "state-hardlink.db"
        os.link(db_path, output)

    assert main(["--db", str(db_path), "--json-output", str(output)]) == 2
    assert "must not be the selected state DB" in capsys.readouterr().err
    assert db_path.read_bytes() == before
    connection = sqlite3.connect(db_path)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_sql_connection_is_query_only(tmp_path: Path) -> None:
    connection = audit._open_read_only(_state_db(tmp_path))
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute("DELETE FROM messages")
    connection.close()


def test_standard_json_excludes_nondeterministic_diagnostics(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _state_db(tmp_path)
    timings = iter((123.456, 987.654))

    def fake_resolve(tools, toolsets):
        return {"candidate": {"terminal"}}, {}, next(timings)

    monkeypatch.setattr(audit, "resolve_candidates", fake_resolve)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    argv = ["--db", str(db_path), "--candidate-toolsets", "candidate=terminal"]
    assert main([*argv, "--json-output", str(first)]) == 0
    assert main([*argv, "--json-output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    payload = json.loads(first.read_text())
    assert "diagnostics" not in payload
    assert "candidate_registry_discovery_ms" not in payload


def test_standard_availability_is_deterministic_and_address_free(monkeypatch) -> None:
    class Check:
        def __call__(self):
            return True

    class Entry:
        name = "conditional_tool"
        toolset = "conditional"
        check_fn = Check()

    class Registry:
        def get_all_tool_names(self):
            return ["conditional_tool"]

        def get_entry(self, _name):
            return Entry()

    discovery_times = iter((111.0, 999.0))
    monkeypatch.setattr(
        audit, "_load_registry", lambda: (Registry(), next(discovery_times))
    )

    first = audit.measure_availability()
    second = audit.measure_availability()
    assert first == second
    serialized = json.dumps(first, sort_keys=True)
    assert "_ms" not in serialized
    assert "0x" not in serialized


def test_diagnostics_are_opt_in(monkeypatch) -> None:
    class Registry:
        def get_all_tool_names(self):
            return []

    monkeypatch.setattr(audit, "_load_registry", lambda: (Registry(), 12.5))
    availability = audit.measure_availability(include_diagnostics=True)
    assert availability["diagnostics"] == {
        "registry_discovery_ms": 12.5,
        "total_check_ms": 0,
    }
