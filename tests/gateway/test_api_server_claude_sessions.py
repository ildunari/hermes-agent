import asyncio
import json
from unittest.mock import MagicMock

from gateway.claude_sessions import get_claude_session, list_claude_sessions, summarize_claude_transcript
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _sample_rows(session_id="sess-123"):
    return [
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-05-10T10:00:00.000Z",
            "uuid": "u1",
            "promptId": "p1",
            "message": {"role": "user", "content": "look up tokens"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-05-10T10:00:02.000Z",
            "uuid": "a1",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "date"}}
                ],
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 7,
                    "cache_read_input_tokens": 11,
                    "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 2},
                },
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-05-10T10:00:03.000Z",
            "uuid": "tr1",
            "toolUseResult": {"stdout": "Sun May 10"},
            "sourceToolAssistantUUID": "a1",
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "sessionId": session_id,
            "timestamp": "2026-05-10T10:00:04.000Z",
            "uuid": "c1",
            "compact_metadata": {"trigger": "manual", "pre_tokens": 12345},
        },
        {"type": "attachment", "sessionId": session_id, "timestamp": "2026-05-10T10:00:05.000Z", "uuid": "att1"},
    ]


def test_summarize_claude_transcript_extracts_usage_tools_and_events(tmp_path):
    claude_home = tmp_path / ".claude"
    transcript = claude_home / "projects" / "-tmp-project" / "sess-123.jsonl"
    _write_jsonl(transcript, _sample_rows())
    (transcript.with_suffix("") / "subagents").mkdir(parents=True)
    (transcript.with_suffix("") / "subagents" / "agent-a.jsonl").write_text("{}\n")

    summary = summarize_claude_transcript(transcript, claude_home=claude_home)

    assert summary["ok"] is True
    assert summary["session_id"] == "sess-123"
    assert summary["project_slug"] == "-tmp-project"
    assert summary["prompt_count"] == 2
    assert summary["assistant_turn_count"] == 1
    assert summary["attachment_count"] == 1
    assert summary["compact_count"] == 1
    assert summary["subagent_count"] == 1
    assert summary["usage"]["input_tokens"] == 3
    assert summary["usage"]["output_tokens"] == 5
    assert summary["usage"]["cache_creation_input_tokens"] == 7
    assert summary["usage"]["cache_read_input_tokens"] == 11
    assert summary["usage"]["web_search_requests"] == 1
    assert summary["usage"]["web_fetch_requests"] == 2
    assert summary["tool_call_counts"] == {"Bash": 1}
    assert summary["tool_result_bytes"] > 0
    assert any(event["kind"] == "tool_call" for event in summary["timeline"])
    assert any(event["kind"] == "compact_boundary" for event in summary["timeline"])


def test_list_and_get_claude_sessions_use_tmp_claude_home(tmp_path):
    claude_home = tmp_path / ".claude"
    transcript = claude_home / "projects" / "-tmp-project" / "sess-abc.jsonl"
    _write_jsonl(transcript, _sample_rows("sess-abc"))

    listed = list_claude_sessions(claude_home=claude_home)
    assert listed["ok"] is True
    assert [s["session_id"] for s in listed["sessions"]] == ["sess-abc"]

    detail = get_claude_session("sess-abc", claude_home=claude_home)
    assert detail["ok"] is True
    assert detail["timeline"]

    missing = get_claude_session("missing", claude_home=claude_home)
    assert missing["ok"] is False
    assert "session_not_found" in missing["warnings"]


def test_handle_claude_sessions_returns_payload(monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, token="test-key"))
    request = MagicMock()
    request.headers = {}
    request.query = {"limit": "5"}
    monkeypatch.setattr(adapter, "_check_auth", lambda request: None)
    monkeypatch.setattr("gateway.platforms.api_server.list_claude_sessions", lambda limit=30: {"ok": True, "limit": limit, "sessions": []})

    response = asyncio.run(adapter._handle_claude_sessions(request))
    assert response.status == 200
    assert json.loads(response.text) == {"ok": True, "limit": 5, "sessions": []}


def test_handle_claude_session_events_returns_timeline(monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, token="test-key"))
    request = MagicMock()
    request.headers = {}
    request.match_info = {"session_id": "sess-123"}
    monkeypatch.setattr(adapter, "_check_auth", lambda request: None)
    monkeypatch.setattr(
        "gateway.platforms.api_server.get_claude_session",
        lambda session_id, include_events=True: {"ok": True, "session_id": session_id, "timeline": [{"kind": "tool_call"}], "warnings": []},
    )

    response = asyncio.run(adapter._handle_claude_session_events(request))
    assert response.status == 200
    assert json.loads(response.text)["timeline"] == [{"kind": "tool_call"}]
