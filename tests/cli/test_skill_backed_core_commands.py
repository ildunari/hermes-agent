"""Regression coverage for profile-owned workflows advertised as core commands."""

import queue

import pytest

from cli import HermesCLI


@pytest.mark.parametrize(
    ("command", "expected_skill", "expected_instruction"),
    [
        ("/update-smart", "update-smart", ""),
        ("/update_smart only update Desktop", "update-smart", "only update Desktop"),
        ("/update-desktop", "update-desktop", ""),
        ("/codex fix it", "codex-cli-lane", "fix it"),
        ("/claude review it", "claude_KM", "review it"),
        ("/cc review it", "claude_KM", "review it"),
        ("/antigravity polish it", "antigravity", "polish it"),
    ],
)
def test_cli_dispatches_skill_backed_core_commands(
    monkeypatch, command, expected_skill, expected_instruction
):
    calls = []

    def fake_build(skill_name, instruction, task_id=None, runtime_note=""):
        calls.append((skill_name, instruction, task_id))
        return f"loaded:{skill_name}:{instruction}"

    monkeypatch.setattr(
        "agent.skill_commands.build_named_skill_invocation_message", fake_build
    )

    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_resume_sessions = None
    cli._pending_input = queue.Queue()
    cli.session_id = "desktop-session"

    assert cli.process_command(command) is True
    assert calls == [(expected_skill, expected_instruction, "desktop-session")]
    assert cli._pending_input.get_nowait() == (
        f"loaded:{expected_skill}:{expected_instruction}"
    )


def test_cli_reports_unavailable_skill_backed_core_command(monkeypatch, capsys):
    monkeypatch.setattr(
        "agent.skill_commands.build_named_skill_invocation_message",
        lambda *args, **kwargs: None,
    )

    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_resume_sessions = None
    cli._pending_input = queue.Queue()
    cli.session_id = "desktop-session"

    assert cli.process_command("/update-smart") is True
    assert cli._pending_input.empty()
    assert "workflow skill is unavailable" in capsys.readouterr().out
