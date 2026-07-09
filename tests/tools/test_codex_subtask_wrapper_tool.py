from __future__ import annotations

import json

from tools import codex_subtask_tool as tool
from tools.registry import registry
from toolsets import resolve_toolset


def test_codex_subtask_create_defaults_and_lifecycle_actions(monkeypatch, tmp_path):
    calls = []

    def fake_request(action, **params):
        calls.append((action, params))
        return {"status": "ok", "action": action, "params": params}

    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(tool.client, "request", fake_request)
    result = json.loads(tool._call({"prompt": "do work", "cwd": str(tmp_path)}))
    assert result["status"] == "ok"
    assert calls[-1][0] == "submit"
    assert calls[-1][1]["mode"] == "sync"
    assert calls[-1][1]["profile"] == "gpt"
    assert calls[-1][1]["hermes_session_id"] == "default"
    assert calls[-1][1]["_socket_timeout"] == 620

    result = json.loads(tool._call({"action": "logs", "job_id": "job_1", "since": 2, "limit": 5, "prompt": "ignored"}))
    assert result["action"] == "logs"
    assert calls[-1] == ("logs", {"job_id": "job_1", "since": 2, "limit": 5})


def test_codex_subtask_action_validation_is_helpful(monkeypatch):
    monkeypatch.setattr(tool.client, "request", lambda *args, **kwargs: {"status": "should-not-call"})

    assert json.loads(tool._call({"action": "create"}))["error"] == 'codex_subtask action="create" requires non-empty prompt'
    assert json.loads(tool._call({"action": "status"}))["error"] == 'codex_subtask action="status" requires job_id'
    assert json.loads(tool._call({"action": "send", "job_id": "job_1"}))["error"] == 'codex_subtask action="send" requires non-empty message'
    assert "submit" not in tool.CODEX_SUBTASK_SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert "create" in tool.CODEX_SUBTASK_SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert json.loads(tool._call({"action": "list", "limit": 201}))["error"] == 'codex_subtask action="list" supports limit <= 200'
    assert "unknown codex_subtask action" in json.loads(tool._call({"action": "wat"}))["error"]


def test_codex_toolset_exposes_single_visible_codex_tool_but_aliases_dispatch(monkeypatch):
    assert resolve_toolset("codex") == ["codex_subtask"]

    calls = []

    def fake_request(action, **params):
        calls.append((action, params))
        return {"status": "ok", "action": action}

    monkeypatch.setattr(tool.client, "request", fake_request)
    assert registry.get_entry("codex_subtask_await") is not None
    result = json.loads(registry.dispatch("codex_subtask_await", {"job_id": "job_1", "timeout_seconds": 1}))
    assert result == {"status": "ok", "action": "await"}
    assert calls[-1] == ("await", {"job_id": "job_1", "timeout_seconds": 1})
