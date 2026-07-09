"""Tests for the messaging /cwd command."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
        user_name="tester",
    )


def _make_entry(cwd_override=None) -> SessionEntry:
    return SessionEntry(
        session_key="sess-key",
        session_id="sess-123",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=_make_source(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        cwd_override=cwd_override,
    )


@pytest.mark.asyncio
async def test_cwd_without_args_shows_current_binding(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _make_entry("/tmp/project-a")
    runner._session_key_for_source = lambda _source: "sess-key"

    event = MessageEvent(text="/cwd", source=_make_source(), message_id="m1")
    result = await GatewayRunner._handle_cwd_command(runner, event)

    assert "/tmp/project-a" in result
    assert "session override" in result


@pytest.mark.asyncio
async def test_cwd_set_updates_binding_and_resets_session(tmp_path):
    from gateway.run import GatewayRunner

    project_dir = tmp_path / "project-b"
    project_dir.mkdir()

    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _make_entry()
    runner.session_store.set_session_cwd = MagicMock()
    runner._session_key_for_source = lambda _source: "sess-key"
    runner._handle_reset_command = AsyncMock(return_value="reset ok")
    runner._clear_task_cwd = MagicMock()

    event = MessageEvent(text=f"/cwd {project_dir}", source=_make_source(), message_id="m2")
    result = await GatewayRunner._handle_cwd_command(runner, event)

    runner.session_store.set_session_cwd.assert_called_once_with("sess-key", str(project_dir.resolve()))
    runner._handle_reset_command.assert_awaited_once_with(event)
    assert "reset ok" in result
    assert str(project_dir.resolve()) in result


@pytest.mark.asyncio
async def test_cwd_bare_relative_path_falls_back_to_home_when_session_path_misses(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    home = tmp_path / "home"
    session_dir = tmp_path / "session"
    project_dir = home / "LocalDev" / "craft-ios-companion"
    project_dir.mkdir(parents=True)
    session_dir.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _make_entry(str(session_dir))
    runner.session_store.set_session_cwd = MagicMock()
    runner._session_key_for_source = lambda _source: "sess-key"
    runner._handle_reset_command = AsyncMock(return_value="reset ok")
    runner._clear_task_cwd = MagicMock()

    event = MessageEvent(text="/cwd LocalDev/craft-ios-companion", source=_make_source(), message_id="m3")
    result = await GatewayRunner._handle_cwd_command(runner, event)

    runner.session_store.set_session_cwd.assert_called_once_with("sess-key", str(project_dir.resolve()))
    runner._handle_reset_command.assert_awaited_once_with(event)
    assert str(project_dir.resolve()) in result


@pytest.mark.asyncio
async def test_cwd_explicit_dot_relative_does_not_fall_back_to_home(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    home = tmp_path / "home"
    session_dir = tmp_path / "session"
    (home / "project-b").mkdir(parents=True)
    session_dir.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _make_entry(str(session_dir))
    runner._session_key_for_source = lambda _source: "sess-key"

    event = MessageEvent(text="/cwd ./project-b", source=_make_source(), message_id="m4")
    result = await GatewayRunner._handle_cwd_command(runner, event)

    assert result == f"Directory not found: {(session_dir / 'project-b').resolve()}"
