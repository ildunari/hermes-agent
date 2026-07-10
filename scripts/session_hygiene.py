#!/usr/bin/env python3
"""Hermes smoke-session finalization, launcher reminders, and child retention.

Canonical source only. Profile deployment is intentionally a later, explicit step.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

RETENTION_SECONDS = 7 * 86400
REMINDER = "Smoke-test reminder: rerun full Hermes agent commands with --source smoke-test."
STRICT_LEGACY_SMOKE_RE = re.compile(
    r"(?is)\bsmoke(?:[- ]\w+)*\b.*\bdo\s+not\s+use\s+tools\b.*\banswer\s+exactly\s*:\s*\S+"
)
# Deliberately require a full Hermes agent invocation, not prose mentioning Hermes.
HERMES_COMMAND_RE = re.compile(
    r"(?:^|[;&|\n]\s*)(?:\S*/)?hermes\s+"
    r"(?:--profile\s+\S+\s+)?(?:chat|run|ask|-q\b|-z\b)",
    re.I,
)


def profile_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser()
    # Deployed profile copies live at <profile>/hooks/session-hygiene/script.py.
    candidate = Path(__file__).resolve().parents[2]
    return candidate if (candidate / "state.db").exists() else Path.home() / ".hermes"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def first_message(conn: sqlite3.Connection, sid: str, role: str) -> str:
    row = conn.execute(
        "SELECT content FROM messages WHERE session_id=? AND role=? AND content IS NOT NULL "
        "ORDER BY timestamp,id LIMIT 1", (sid, role),
    ).fetchone()
    return str(row[0] or "") if row else ""


def legacy_match(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Preserve the pre-marker fail-closed matcher byte-for-byte in substance."""
    if (row["source"] or "") != "cli" or row["parent_session_id"]:
        return False
    if int(row["message_count"] or 0) > 4:
        return False
    try:
        cfg = json.loads(row["model_config"] or "{}")
    except Exception:
        cfg = {}
    if cfg.get("_delegate_from") or cfg.get("_branched_from"):
        return False
    user = first_message(conn, row["id"], "user").strip()
    assistant = first_message(conn, row["id"], "assistant").strip()
    return bool(
        STRICT_LEGACY_SMOKE_RE.search(user)
        and assistant and len(assistant) <= 200
        and ("\n" not in assistant or len(assistant.splitlines()) <= 3)
    )


def finalize_and_maintain(conn: sqlite3.Connection, sid: str, now: float) -> dict[str, int | bool]:
    """Atomically archive an ended smoke row and stale ended subagents."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id,source,parent_session_id,message_count,archived,model_config,ended_at "
            "FROM sessions WHERE id=?", (sid,),
        ).fetchone()
        smoke = False
        if row and not int(row["archived"] or 0) and row["ended_at"] is not None:
            smoke = (row["source"] == "smoke-test") or legacy_match(conn, row)
            if smoke:
                conn.execute("UPDATE sessions SET archived=1 WHERE id=? AND ended_at IS NOT NULL", (sid,))
        cutoff = now - RETENTION_SECONDS
        cur = conn.execute(
            "UPDATE sessions SET archived=1 WHERE archived=0 AND source='subagent' "
            "AND ended_at IS NOT NULL AND ended_at<=?", (cutoff,),
        )
        retained = cur.rowcount if cur.rowcount >= 0 else conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return {"smoke_archived": smoke, "subagents_archived": retained}
    except Exception:
        conn.rollback()
        raise


def reminder_for(payload: dict) -> str | None:
    if payload.get("hook_event_name") != "transform_tool_result" or payload.get("tool_name") != "terminal":
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not HERMES_COMMAND_RE.search(command) or re.search(r"(?:^|\s)--source(?:=|\s+)smoke-test(?:\s|$)", command):
        return None
    result = str((payload.get("extra") or {}).get("result") or "")
    # Per-result deduplication makes retries/composed hooks idempotent.
    if REMINDER in result:
        return result
    return f"{result.rstrip()}\n\n{REMINDER}" if result.strip() else REMINDER


def runtime(db_path: Path, now: float) -> dict:
    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name")
    if event == "transform_tool_result":
        replacement = reminder_for(payload)
        return {"result": replacement} if replacement is not None else {}
    if event == "on_session_finalize":
        sid = str(payload.get("session_id") or "").strip()
        if sid and sid != "test-session":
            with connect(db_path) as conn:
                finalize_and_maintain(conn, sid, now)
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()
    try:
        out = runtime(args.db or profile_home() / "state.db", args.now or time.time())
    except Exception:
        out = {}
    print(json.dumps(out, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
