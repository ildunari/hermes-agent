"""Behavior/security tests for the plugin command invocation context."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.command_context import (
    build_gateway_command_context,
    dispatch_pending_plugin_command_followup,
)
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource
from hermes_cli.command_context import (
    CommandCapabilityError,
    CommandInvocationContext,
)
from hermes_cli.plugins import (
    PluginContext,
    PluginManager,
    PluginManifest,
    invoke_plugin_command,
)


def _source(*, profile="alpha", thread_id="topic-1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="1",
        chat_type="dm",
        thread_id=thread_id,
        profile=profile,
    )


def _entry(source: SessionSource, key: str = "session-key") -> SessionEntry:
    now = datetime(2026, 8, 31, 12, 0, 0)
    return SessionEntry(
        session_key=key,
        session_id=f"id-{key}",
        created_at=now,
        updated_at=now,
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
        cwd_override="/work/alpha",
        personality_override={"name": "pirate", "prompt": "Ahoy"},
    )


class _AsyncStore:
    def __init__(self, current, entries):
        self.current = current
        self.entries = entries

    async def get_or_create_session(self, source, **kwargs):
        return self.current

    async def get_session(self, key):
        return self.current if key == self.current.session_key else None

    async def list_sessions(self):
        return list(self.entries)

    async def set_session_cwd(self, key, cwd):
        self.current.cwd_override = cwd
        return self.current

    async def set_session_personality_override(self, key, value):
        self.current.personality_override = value
        return self.current


class _Runner:
    def __init__(self, current, entries, adapter=None):
        self.async_session_store = _AsyncStore(current, entries)
        self._adapter = adapter
        self._session_personality_overrides = {}
        self._session_entry_cache = {}
        self._session_model_overrides = {}
        self._session_db = None
        self.evicted = []

    def _adapter_for_source(self, source):
        return self._adapter

    def _session_cwd_for_entry(self, entry):
        return entry.cwd_override or "/gateway/default"

    def _session_key_for_source(self, source):
        return f"key:{source.profile}:{source.chat_id}:{source.thread_id}"

    async def _handle_reset_command(self, event, preserve_session_config=False):
        return "reset ok"

    def _evict_cached_agent(self, key):
        self.evicted.append(key)

    async def _send_voice_reply(self, event, text):
        return None


@pytest.fixture
def registered_manager(monkeypatch):
    manager = PluginManager()
    manifest = PluginManifest(name="context-probe", source="user")
    context = PluginContext(manifest, manager)
    monkeypatch.setattr("hermes_cli.plugins._ensure_plugins_discovered", lambda force=False: manager)
    return manager, context


def test_legacy_handler_still_receives_raw_args(registered_manager):
    _, registration = registered_manager
    seen = []
    registration.register_command("legacy-probe", lambda raw: seen.append(raw) or "ok")

    assert invoke_plugin_command("legacy-probe", "  exact args  ") == "ok"
    assert seen == ["  exact args  "]


def test_context_handler_gets_capability_free_cli_context(registered_manager):
    _, registration = registered_manager
    seen = []
    registration.register_command(
        "context-probe",
        lambda invocation: seen.append(invocation) or invocation.surface,
        context=True,
    )

    from hermes_cli.commands import is_gateway_known_command

    assert is_gateway_known_command("context_probe")
    assert invoke_plugin_command("context-probe", "hello") == "cli"
    invocation = seen[0]
    assert isinstance(invocation, CommandInvocationContext)
    assert invocation.raw_args == "hello"
    assert invocation.capabilities == frozenset()


@pytest.mark.asyncio
async def test_gateway_context_is_typed_immutable_and_hides_host_internals(
    registered_manager,
):
    _, registration = registered_manager
    source = _source()
    event = MessageEvent(text="/context-probe hello", source=source, message_id="m1")
    runner = _Runner(_entry(source), [_entry(source)])
    invocation = await build_gateway_command_context(
        runner, event, "context-probe", "hello"
    )
    registration.register_command(
        "context-probe", lambda value: value, context=True
    )

    received = invoke_plugin_command(
        "context-probe", "hello", context=invocation
    )
    assert received is invocation
    assert invocation.profile == "alpha"
    assert invocation.source is not None
    assert invocation.source.platform == "telegram"
    assert invocation.session is not None
    assert invocation.session.session_id == "id-session-key"
    assert not hasattr(invocation, "gateway")
    assert not hasattr(invocation, "runner")
    assert not hasattr(invocation, "adapter")
    assert not hasattr(invocation, "session_store")
    with pytest.raises(Exception):
        setattr(invocation, "profile", "other")


@pytest.mark.asyncio
async def test_session_listing_fails_closed_across_profiles():
    source = _source(profile="alpha")
    alpha = _entry(source, "alpha-key")
    beta = _entry(_source(profile="beta"), "beta-key")
    event = MessageEvent(text="/threads", source=source)
    invocation = await build_gateway_command_context(
        _Runner(alpha, [alpha, beta]), event, "threads", ""
    )

    visible = await invocation.list_sessions()
    assert [item.session_key for item in visible] == ["alpha-key"]
    assert all(item.profile == "alpha" for item in visible)


@pytest.mark.asyncio
async def test_missing_platform_capability_fails_before_transport_call():
    source = _source()
    event = MessageEvent(text="/newthread Test", source=source)
    invocation = await build_gateway_command_context(
        _Runner(_entry(source), [_entry(source)], adapter=SimpleNamespace()),
        event,
        "newthread",
        "Test",
    )

    assert not invocation.supports("thread.create")
    with pytest.raises(CommandCapabilityError):
        await invocation.create_thread("Test")


@pytest.mark.asyncio
async def test_platform_effect_is_mocked_and_scoped_to_current_chat():
    source = _source()
    current = _entry(source)
    create_topic = AsyncMock(return_value=None)
    adapter = SimpleNamespace(create_topic=create_topic)
    event = MessageEvent(text="/newthread Test", source=source)
    invocation = await build_gateway_command_context(
        _Runner(current, [current], adapter=adapter),
        event,
        "newthread",
        "Test",
    )

    result = await invocation.create_thread("Test")
    assert not result.ok
    create_topic.assert_awaited_once_with(chat_id=1, name="Test", persist=True)


@pytest.mark.asyncio
async def test_generic_followup_reinvokes_context_handler_in_same_profile(
    registered_manager,
):
    _, registration = registered_manager
    seen = []

    async def handler(invocation):
        seen.append((invocation.profile, invocation.raw_args))
        return "followup handled"

    registration.register_command("ask-probe", handler, context=True)
    source = _source(profile="alpha")
    current = _entry(source)
    adapter = SimpleNamespace(send=AsyncMock())
    runner = _Runner(current, [current], adapter=adapter)
    command_event = MessageEvent(text="/ask-probe", source=source, message_id="m1")
    invocation = await build_gateway_command_context(
        runner, command_event, "ask-probe", ""
    )
    prompt_result = await invocation.prompt_for_text("What next?")
    assert prompt_result.ok

    reply_event = MessageEvent(text="same profile reply", source=source, message_id="m2")
    result = await dispatch_pending_plugin_command_followup(
        runner, reply_event, current.session_key
    )

    assert result == "followup handled"
    assert seen == [("alpha", "same profile reply")]
    adapter.send.assert_awaited_once()
