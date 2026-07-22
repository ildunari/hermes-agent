import json
import os
from types import SimpleNamespace

import pytest


class _FakeProc:
    def __init__(self, stdout_fd):
        os.write(stdout_fd, json.dumps({"success": True, "data": {}}).encode())
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.returncode = -9


def _reset_profile_cache(browser_tool):
    browser_tool._cached_agent_browser_profile = None
    browser_tool._agent_browser_profile_resolved = False


@pytest.fixture(autouse=True)
def _isolate_profile_cache():
    from tools import browser_tool

    _reset_profile_cache(browser_tool)
    yield
    _reset_profile_cache(browser_tool)


def test_agent_browser_profile_prefers_profile_config(monkeypatch):
    from tools import browser_tool

    _reset_profile_cache(browser_tool)
    monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"browser": {"agent_browser_profile": "Default"}},
    )

    assert browser_tool._get_agent_browser_profile() == "Default"


def test_agent_browser_profile_falls_back_to_env(monkeypatch):
    from tools import browser_tool

    _reset_profile_cache(browser_tool)
    monkeypatch.setenv("AGENT_BROWSER_PROFILE", "Default")
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {"browser": {}})

    assert browser_tool._get_agent_browser_profile() == "Default"


def test_local_agent_browser_command_includes_configured_profile(monkeypatch, tmp_path):
    from tools import browser_tool

    captured = {}

    def fake_popen(cmd_parts, stdout, stderr, stdin, env, **kwargs):
        captured["cmd_parts"] = cmd_parts
        captured["env"] = env
        return _FakeProc(stdout)

    _reset_profile_cache(browser_tool)
    monkeypatch.setattr(browser_tool, "_find_agent_browser", lambda: "/usr/local/bin/agent-browser")
    monkeypatch.setattr(browser_tool, "_requires_real_termux_browser_install", lambda browser_cmd: False)
    monkeypatch.setattr(browser_tool, "_is_local_mode", lambda: True)
    monkeypatch.setattr(browser_tool, "_chromium_installed", lambda: True)
    monkeypatch.setattr(browser_tool, "_get_session_info", lambda task_id: {"session_name": "task-session"})
    monkeypatch.setattr(browser_tool, "_get_browser_engine", lambda: "chrome")
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_socket_safe_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(browser_tool, "_write_owner_pid", lambda socket_dir, session_name: None)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {"browser": {"agent_browser_profile": "Default"}})
    monkeypatch.setattr(browser_tool.subprocess, "Popen", fake_popen)

    result = browser_tool._run_browser_command("task-1", "snapshot")

    assert result["success"] is True
    assert captured["cmd_parts"][:7] == [
        "/usr/local/bin/agent-browser",
        "--profile",
        "Default",
        "--session",
        "task-session",
        "--engine",
        "chrome",
    ]
