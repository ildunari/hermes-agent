"""Tests for gateway /personality_session command isolation."""

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import yaml

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, SessionStore


def _make_event(text="/personality_session", platform=Platform.DISCORD, user_id="user-1", chat_id="thread-1"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        chat_type="thread" if platform == Platform.DISCORD else "dm",
        user_name="Kosta",
        thread_id="thread-1" if platform == Platform.DISCORD else None,
        parent_chat_id="parent-1" if platform == Platform.DISCORD else None,
        guild_id="guild-1" if platform == Platform.DISCORD else None,
    )
    return MessageEvent(text=text, source=source)


def _make_runner(sessions_dir=None):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = "GLOBAL PROFILE PROMPT"
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._session_personality_overrides = {}
    runner._session_model_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._session_entry_cache = {}
    config = GatewayConfig(sessions_dir=Path(sessions_dir or "/tmp/hermes-personality-session-tests"))
    runner.config = config
    runner.session_store = SessionStore(config.sessions_dir, config)
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    return runner


class _CapturingAgent:
    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        return {"final_response": "ok", "messages": [], "api_calls": 1}


def test_personality_session_is_known_command():
    from hermes_cli.commands import resolve_command

    assert resolve_command("personality_session").name == "personality_session"
    assert resolve_command("personality-session").name == "personality_session"


def test_discord_slash_personality_session_registered():
    source = (Path(__file__).parents[2] / "plugins/platforms/discord/adapter.py").read_text(
        encoding="utf-8"
    )

    assert 'tree.command(name="personality_session"' in source
    assert '"/personality_session {name}"' in source


async def _set_personality_session(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "agent:\n"
        "  system_prompt: GLOBAL PROFILE PROMPT\n"
        "  personalities:\n"
        "    pirate:\n"
        "      description: Talk like a pirate\n"
        "      system_prompt: SESSION PIRATE PROMPT\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    runner = _make_runner(hermes_home / "sessions")
    event = _make_event("/personality_session pirate")

    result = await runner._handle_personality_session_command(event)
    return runner, event, config_path, result


def test_personality_session_sets_session_override_without_config_write(tmp_path, monkeypatch):
    runner, event, config_path, result = asyncio.run(_set_personality_session(tmp_path, monkeypatch))
    session_key = runner._session_key_for_source(event.source)
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert saved["agent"]["system_prompt"] == "GLOBAL PROFILE PROMPT"
    assert runner._session_personality_overrides[session_key] == {
        "name": "pirate",
        "prompt": "SESSION PIRATE PROMPT",
    }
    assert "chat/thread only" in result
    assert "Profile config was not changed" in result


def test_personality_session_persists_to_current_session_entry(tmp_path, monkeypatch):
    runner, event, _, _ = asyncio.run(_set_personality_session(tmp_path, monkeypatch))
    session_key = runner._session_key_for_source(event.source)

    reloaded_store = SessionStore(tmp_path / "hermes" / "sessions", runner.config)
    reloaded_entry = reloaded_store.get_session(session_key)

    assert reloaded_entry is not None
    assert reloaded_entry.session_key == session_key
    assert reloaded_entry.personality_override == {
        "name": "pirate",
        "prompt": "SESSION PIRATE PROMPT",
    }


def test_session_store_reset_clears_or_preserves_personality_override(tmp_path, monkeypatch):
    runner, event, _, _ = asyncio.run(_set_personality_session(tmp_path, monkeypatch))
    session_key = runner._session_key_for_source(event.source)

    cleared_entry = runner.session_store.reset_session(session_key)
    assert cleared_entry is not None
    assert cleared_entry.personality_override is None

    runner.session_store.set_session_personality_override(
        session_key,
        {"name": "pirate", "prompt": "SESSION PIRATE PROMPT"},
    )
    preserved_entry = runner.session_store.reset_session(session_key, preserve_session_config=True)
    assert preserved_entry is not None
    assert preserved_entry.personality_override == {
        "name": "pirate",
        "prompt": "SESSION PIRATE PROMPT",
    }


def test_personality_session_clear_removes_only_session_override(tmp_path, monkeypatch):
    runner, event, config_path, _ = asyncio.run(_set_personality_session(tmp_path, monkeypatch))
    session_key = runner._session_key_for_source(event.source)

    result = asyncio.run(runner._handle_personality_session_command(_make_event("/personality_session none")))
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert saved["agent"]["system_prompt"] == "GLOBAL PROFILE PROMPT"
    assert session_key not in runner._session_personality_overrides
    assert "cleared" in result


def test_run_agent_appends_session_personality_after_profile_prompt(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("agent:\n  system_prompt: GLOBAL PROFILE PROMPT\n", encoding="utf-8")

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(gateway_run, "_env_path", hermes_home / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
        },
    )
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    runner = _make_runner(hermes_home / "sessions")
    source = _make_event("hello").source
    session_key = runner._session_key_for_source(source)
    runner._session_personality_overrides[session_key] = {
        "name": "pirate",
        "prompt": "SESSION PIRATE PROMPT",
    }

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="THREAD CONTEXT",
            history=[],
            source=source,
            session_id="session-1",
            session_key=session_key,
        )
    )

    assert result["final_response"] == "ok"
    prompt = _CapturingAgent.last_init["ephemeral_system_prompt"]
    assert "THREAD CONTEXT" in prompt
    assert "GLOBAL PROFILE PROMPT" in prompt
    assert "SESSION PIRATE PROMPT" in prompt
    assert prompt.index("GLOBAL PROFILE PROMPT") < prompt.index("SESSION PIRATE PROMPT")
