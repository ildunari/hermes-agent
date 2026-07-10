from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "session_hygiene.py"
spec = importlib.util.spec_from_file_location("session_hygiene", SCRIPT)
assert spec is not None and spec.loader is not None
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)


def make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, message_count INTEGER, archived INTEGER, model_config TEXT, ended_at REAL)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    return conn


def add(conn, sid, source, ended_at, *, archived=0, user="ordinary", assistant="ok", parent=None):
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (sid, source, parent, 2, archived, "{}", ended_at))
    conn.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,1)", (sid, "user", user))
    conn.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,2)", (sid, "assistant", assistant))
    conn.commit()


def test_finalize_marker_and_seven_day_retention_preserve_messages(tmp_path):
    db = tmp_path / "state.db"
    conn = make_db(db)
    now = 2_000_000_000.0
    add(conn, "marked", "smoke-test", now - 1)
    add(conn, "active-marked", "smoke-test", None)
    add(conn, "old-child", "subagent", now - hygiene.RETENTION_SECONDS - 1)
    add(conn, "edge-child", "subagent", now - hygiene.RETENTION_SECONDS)
    add(conn, "young-child", "subagent", now - hygiene.RETENTION_SECONDS + 1)
    add(conn, "active-child", "subagent", None)
    before = conn.execute("SELECT count(*) FROM messages").fetchone()[0]

    outcome = hygiene.finalize_and_maintain(conn, "marked", now)

    rows = dict(conn.execute("SELECT id,archived FROM sessions"))
    assert outcome == {"smoke_archived": True, "subagents_archived": 2}
    assert rows == {"marked": 1, "active-marked": 0, "old-child": 1, "edge-child": 1, "young-child": 0, "active-child": 0}
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == before


def test_strict_legacy_matcher_is_preserved_and_fail_closed(tmp_path):
    conn = make_db(tmp_path / "state.db")
    now = time.time()
    strict = "Read-only smoke check. Do not use tools. Answer exactly: PASS"
    add(conn, "legacy", "cli", now - 1, user=strict, assistant="PASS")
    add(conn, "loose", "cli", now - 1, user="Please smoke test this normal debugging session", assistant="done")
    assert hygiene.finalize_and_maintain(conn, "legacy", now)["smoke_archived"] is True
    assert hygiene.finalize_and_maintain(conn, "loose", now)["smoke_archived"] is False


def payload(command: str, result: str = "ok") -> dict:
    return {"hook_event_name": "transform_tool_result", "tool_name": "terminal", "tool_input": {"command": command}, "extra": {"result": result}, "session_id": "parent"}


def test_reminder_recognizes_full_commands_only_and_deduplicates():
    reminded = hygiene.reminder_for(payload("hermes --profile coding chat -q 'probe'"))
    assert reminded == "ok\n\n" + hygiene.REMINDER
    assert hygiene.reminder_for(payload("hermes chat --source smoke-test -q probe")) is None
    assert hygiene.reminder_for(payload("hermes -q 'probe'")) == "ok\n\n" + hygiene.REMINDER
    assert hygiene.reminder_for(payload("hermes --profile coding -z 'probe'")) == "ok\n\n" + hygiene.REMINDER
    assert hygiene.reminder_for(payload("printf 'mention hermes chat in prose'")) is None
    assert hygiene.reminder_for(payload("hermes chat -q probe", reminded)) == reminded


def test_runtime_uses_temp_db_and_emits_transform_shape(tmp_path):
    db = tmp_path / "state.db"
    conn = make_db(db)
    add(conn, "marked", "smoke-test", 100)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--now", "200"],
        input=json.dumps(payload("hermes run -q probe")), text=True, capture_output=True, check=True,
    )
    assert json.loads(proc.stdout)["result"].endswith(hygiene.REMINDER)
