"""Regression coverage for skill aliases dispatched through the generic CLI path."""

import queue

import pytest

import cli as cli_module
from cli import HermesCLI


@pytest.mark.parametrize(
    ("command", "resolved_key", "expected_instruction"),
    [
        ("/update_smart only update Desktop", "/update-smart", "only update Desktop"),
        ("/codex fix it", "/codex-cli-lane", "fix it"),
        ("/claude review it", "/claude-km", "review it"),
        ("/cc review it", "/claude-km", "review it"),
    ],
)
def test_cli_dispatches_skill_aliases(
    monkeypatch, command, resolved_key, expected_instruction
):
    commands = {
        resolved_key: {
            "name": resolved_key.lstrip("/"),
            "description": "Test skill",
        }
    }
    calls = []

    monkeypatch.setattr(cli_module, "_ensure_skill_commands", lambda: commands)
    monkeypatch.setattr(cli_module, "get_skill_bundles", lambda: {})
    monkeypatch.setattr(cli_module, "_get_plugin_cmd_handler_names", lambda: set())
    monkeypatch.setattr(
        "agent.skill_commands.resolve_skill_command_key",
        lambda typed: resolved_key,
    )

    def fake_build(key, instruction, task_id=None, runtime_note=""):
        calls.append((key, instruction, task_id))
        return f"loaded:{key}:{instruction}"

    monkeypatch.setattr(
        "agent.skill_commands.build_skill_invocation_message", fake_build
    )

    instance = HermesCLI.__new__(HermesCLI)
    instance._pending_resume_sessions = None
    instance._pending_input = queue.Queue()
    instance.session_id = "desktop-session"
    instance.config = {}

    assert instance.process_command(command) is True
    assert calls == [(resolved_key, expected_instruction, "desktop-session")]
    assert instance._pending_input.get_nowait() == (
        f"loaded:{resolved_key}:{expected_instruction}"
    )
