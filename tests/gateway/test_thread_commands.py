"""Tests for Telegram thread/cwd UX commands."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _source(thread_id=None, chat_topic=None):
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="67890",
        user_name="tester",
        chat_type="dm",
        thread_id=thread_id,
        chat_topic=chat_topic,
    )


def _event(text="/threads", source=None):
    return MessageEvent(text=text, source=source or _source())


def _entry(key, source, cwd=None, name=None):
    now = datetime(2026, 5, 13, 1, 0, 0)
    return SessionEntry(
        session_key=key,
        session_id="sid-" + key.replace(":", "-"),
        created_at=now,
        updated_at=now,
        origin=source,
        display_name=name,
        platform=source.platform,
        chat_type=source.chat_type,
        cwd_override=cwd,
    )


def _runner(entries=None):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._pending_newthread_prompts = {}
    runner._session_key_for_source = lambda source: f"key:{source.chat_id}:{source.thread_id or 'root'}"
    store = MagicMock()
    store._entries = {e.session_key: e for e in (entries or [])}
    store._lock = None
    store._ensure_loaded_locked = lambda: None
    runner.session_store = store
    return runner


@pytest.mark.asyncio
async def test_threads_lists_current_topic_and_cwd(tmp_path):
    current = _source(thread_id="100", chat_topic="craft · Hermes")
    other = _source(thread_id="200", chat_topic="notes · Hermes")
    runner = _runner([
        _entry("key:67890:100", current, cwd=str(tmp_path / "craft")),
        _entry("key:67890:200", other, cwd=None),
    ])

    result = await runner._handle_threads_command(_event("/threads", current))

    assert "Known thread bindings:" in result
    assert "craft · Hermes (100):" in result
    assert "current" in result
    assert "notes · Hermes (200): default" in result


@pytest.mark.asyncio
async def test_thread_close_clears_current_cwd_binding():
    source = _source(thread_id="100", chat_topic="craft · Hermes")
    runner = _runner([_entry("key:67890:100", source, cwd="/tmp/craft")])

    result = await runner._handle_thread_command(_event("/thread close", source))

    runner.session_store.set_session_cwd.assert_called_once_with("key:67890:100", None)
    assert "binding cleared" in result


@pytest.mark.asyncio
async def test_thread_new_creates_topic_and_binds_cwd(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = _source()
    runner = _runner([_entry("key:67890:root", source)])
    adapter = SimpleNamespace(create_topic=AsyncMock(return_value=2468), send=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_id="sess-new",
        session_key="key:67890:2468",
    )

    result = await runner._handle_thread_command(_event(f"/thread new {project} Project chat", source))

    assert result is None
    adapter.create_topic.assert_awaited_once_with(chat_id=67890, name="Project chat", persist=True)
    runner.session_store.set_session_cwd.assert_called_with("key:67890:2468", str(project.resolve()))
