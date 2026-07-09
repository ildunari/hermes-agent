"""Tests for gateway session reset/clear model override behavior."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_thread_source(thread_id: str = "12332") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
        thread_id=thread_id,
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_thread_event(text: str, thread_id: str = "12332") -> MessageEvent:
    return MessageEvent(text=text, source=_make_thread_source(thread_id), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.reset_session.return_value = session_entry
    runner.session_store.get_session.return_value = session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None  # disables _evict_cached_agent lock path
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
async def test_new_command_clears_session_model_override():
    """/new must remove the session-scoped model override for that session."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    # Simulate a prior /model switch stored as a session override
    runner._session_model_overrides[session_key] = {
        "model": "gpt-4o",
        "provider": "openai",
        "api_key": "***",
        "base_url": "",
        "api_mode": "openai",
    }
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}
    runner._pending_model_notes[session_key] = "[Note: switched to gpt-4o.]"

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides
    assert session_key not in runner._pending_model_notes


@pytest.mark.asyncio
async def test_new_command_no_override_is_noop():
    """/new with no prior model override must not raise."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides


@pytest.mark.asyncio
async def test_new_command_only_clears_own_session():
    """/new must only clear the override for the session that triggered it."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())
    other_key = "other_session_key"

    runner._session_model_overrides[session_key] = {
        "model": "gpt-4o",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "",
        "api_mode": "openai",
    }
    runner._session_model_overrides[other_key] = {
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "api_key": "***",
        "base_url": "",
        "api_mode": "anthropic",
    }
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}
    runner._session_reasoning_overrides[other_key] = {"enabled": True, "effort": "low"}
    runner._pending_model_notes[session_key] = "[Note: switched to gpt-4o.]"
    runner._pending_model_notes[other_key] = "[Note: switched to claude-sonnet-4-6.]"

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert other_key in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides
    assert other_key in runner._session_reasoning_overrides
    assert session_key not in runner._pending_model_notes
    assert other_key in runner._pending_model_notes


@pytest.mark.asyncio
async def test_clear_command_preserves_session_model_and_reasoning_overrides():
    """/clear starts fresh history while keeping session-scoped config."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    model_override = {
        "model": "gpt-5.5",
        "provider": "openai-codex",
        "api_key": "***",
        "base_url": "",
        "api_mode": "codex_responses",
    }
    reasoning_override = {"enabled": True, "effort": "high"}
    model_note = "[Note: switched to gpt-5.5.]"
    runner._session_model_overrides[session_key] = dict(model_override)
    runner._session_reasoning_overrides[session_key] = dict(reasoning_override)
    runner._pending_model_notes[session_key] = model_note

    response = await runner._handle_reset_command(
        _make_event("/clear"),
        preserve_session_config=True,
    )

    assert runner._session_model_overrides[session_key] == model_override
    assert runner._session_reasoning_overrides[session_key] == reasoning_override
    assert session_key not in runner._pending_model_notes
    runner.session_store.reset_session.assert_called_with(
        session_key,
        preserve_session_config=True,
    )
    assert "preserved" in response


def test_session_runtime_hydrates_persisted_model_override_after_restart():
    """A gateway restart should not lose a thread's session-scoped model."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())
    entry = runner.session_store.get_session.return_value
    entry.model_override = {
        "model": "qwopus-gpu",
        "provider": "custom:rtx",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_mode": "openai",
    }
    runner._session_model_overrides = {}

    model, runtime = runner._resolve_session_agent_runtime(
        session_key=session_key,
        user_config={"model": {"default": "glm-5.1", "provider": "zai"}},
    )

    assert model == "qwopus-gpu"
    assert runtime["provider"] == "custom:rtx"
    assert runtime["base_url"] == "http://127.0.0.1:8000/v1"
    assert runtime["api_mode"] == "openai"
    assert runtime["api_key"] == ""
    assert runner._session_model_overrides[session_key]["model"] == "qwopus-gpu"


@pytest.mark.asyncio
async def test_clear_command_response_reports_preserved_session_model(monkeypatch):
    """/clear confirmation must display the preserved override, not config default."""
    from gateway import run as gateway_run
    from gateway.run import GatewayRunner

    runner = _make_runner()
    session_key = build_session_key(_make_source())
    runner._format_session_info = GatewayRunner._format_session_info.__get__(runner, GatewayRunner)
    runner._session_cwd_for_entry = lambda _entry: "/tmp/project"
    runner._session_model_overrides[session_key] = {
        "model": "qwopus-gpu",
        "provider": "custom:rtx",
        "api_key": "***",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_mode": "openai",
    }

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
        "model": {"default": "glm-5.1", "provider": "zai", "context_length": 200000},
        "custom_providers": [
            {
                "name": "RTX",
                "base_url": "http://127.0.0.1:8000/v1",
                "api_mode": "chat_completions",
                "models": {"qwopus-gpu": {"context_length": 131072}},
            }
        ],
    })
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {
        "provider": "zai",
        "api_key": "zai-key",
        "base_url": "",
        "api_mode": "openai",
    })

    response = await runner._handle_reset_command(
        _make_event("/clear"),
        preserve_session_config=True,
    )
    text = str(response)

    assert "qwopus-gpu" in text
    assert "custom:rtx" in text
    assert "131K tokens" in text
    assert "glm-5.1" not in text


def test_clear_is_gateway_known_command():
    """The central registry should expose /clear to Telegram and other gateways."""
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command, telegram_menu_commands

    assert "clear" in GATEWAY_KNOWN_COMMANDS
    assert resolve_command("clear").cli_only is False
    menu_commands, _hidden = telegram_menu_commands(max_commands=100)
    menu_names = {name for name, _desc in menu_commands}
    assert "clear" in menu_names


@pytest.mark.asyncio
async def test_telegram_model_topic_alias_switches_session_without_agent(monkeypatch):
    """Plain Telegram topic labels can bind a model preset before /clear preserves it."""
    from gateway import run as gateway_run

    runner = _make_runner()
    source = _make_thread_source()
    session_key = build_session_key(source)
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-topic",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        origin=source,
    )
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.get_session.return_value = session_entry
    runner.session_store.reset_session.return_value = session_entry
    runner._format_session_info = lambda: ""

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
        "model": {"default": "glm-5.1", "provider": "zai"},
        "telegram": {
            "model_topic_aliases": {
                "RTX Qwopus": "qwopus-3.6",
            }
        },
    })

    async def fake_model_command(event):
        assert event.text == "/model qwopus-3.6"
        runner._session_model_overrides[session_key] = {
            "model": "qwopus-gpu",
            "provider": "custom:RTX",
            "api_key": "***",
            "base_url": "http://100.93.10.54:8010/v1",
            "api_mode": "chat_completions",
        }
        runner.session_store.set_session_model_override(
            session_key,
            runner._session_model_overrides[session_key],
        )
        return "Switched model to qwopus-gpu"

    runner._handle_model_command = fake_model_command

    result = await runner._maybe_handle_model_topic_alias(_make_thread_event("RTX Qwopus"))

    assert result == "Switched model to qwopus-gpu"
    runner.session_store.set_session_model_override.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_model_topic_alias_ignores_normal_text(monkeypatch):
    """Only exact configured labels are intercepted; normal chat still reaches the agent."""
    from gateway import run as gateway_run

    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
        "telegram": {"model_topic_aliases": {"RTX Qwopus": "qwopus-3.6"}}
    })

    result = await runner._maybe_handle_model_topic_alias(_make_thread_event("RTX Qwopus please search"))

    assert result is None
