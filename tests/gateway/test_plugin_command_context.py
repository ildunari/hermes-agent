"""Behavior/security tests for the plugin command invocation context."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gateway.command_context import (
    build_gateway_command_context,
    dispatch_gateway_plugin_command,
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
    with pytest.raises(TypeError):
        cast(Any, invocation.session.personality_override)["name"] = "stolen"


@pytest.mark.asyncio
async def test_context_rejects_cross_profile_initial_session_resolution():
    source = _source(profile="alpha")
    wrong = _entry(_source(profile="beta"), "shared-looking-key")
    event = MessageEvent(text="/context-probe", source=source)

    with pytest.raises(PermissionError, match="source/profile boundary"):
        await build_gateway_command_context(
            _Runner(wrong, [wrong]), event, "context-probe", ""
        )


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
async def test_non_telegram_same_named_topic_method_is_never_granted_or_called():
    source = _source()
    source.platform = Platform.DISCORD
    current = _entry(source)
    create_topic = AsyncMock(return_value="external-thread")
    adapter = SimpleNamespace(create_topic=create_topic)
    event = MessageEvent(text="/newthread Test", source=source)
    invocation = await build_gateway_command_context(
        _Runner(current, [current], adapter=adapter),
        event,
        "newthread",
        "Test",
    )

    assert not invocation.supports("thread.create")
    with pytest.raises(CommandCapabilityError):
        await invocation.create_thread("Test")
    create_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_telegram_same_named_rename_method_is_never_called():
    source = _source()
    source.platform = Platform.DISCORD
    current = _entry(source)
    rename_topic = AsyncMock()
    adapter = SimpleNamespace(rename_topic=rename_topic)
    invocation = await build_gateway_command_context(
        _Runner(current, [current], adapter=adapter),
        MessageEvent(text="/thread rename New", source=source),
        "thread",
        "rename New",
    )

    result = await invocation.rename_thread("New")

    assert result.ok
    assert result.error_code == "platform_unavailable"
    rename_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_rewritten_plugin_command_falls_through_as_model_input(
    registered_manager,
):
    _, registration = registered_manager

    def rewrite(invocation):
        invocation.rewrite_input("expanded skill invocation")
        return None

    registration.register_command("rewrite-probe", rewrite, context=True)
    source = _source()
    current = _entry(source)
    event = MessageEvent(text="/rewrite-probe hello", source=source)
    runner = _Runner(current, [current])

    result = await dispatch_gateway_plugin_command(
        runner, event, "rewrite-probe"
    )

    assert result.matched
    assert result.continue_as_message
    assert result.response is None
    assert event.text == "expanded skill invocation"


@pytest.mark.asyncio
async def test_gateway_dispatch_preserves_legacy_raw_argument_signature(
    registered_manager,
):
    _, registration = registered_manager
    seen = []
    registration.register_command(
        "legacy-probe", lambda raw: seen.append(raw) or f"legacy:{raw}"
    )
    source = _source()
    event = MessageEvent(text="/legacy-probe exact args", source=source)
    runner = _Runner(_entry(source), [_entry(source)])

    result = await dispatch_gateway_plugin_command(runner, event, "legacy-probe")

    assert result.matched
    assert not result.continue_as_message
    assert result.response == "legacy:exact args"
    assert seen == ["exact args"]


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


@pytest.mark.asyncio
async def test_followup_preserves_command_specific_cancel_message(
    registered_manager,
):
    _, registration = registered_manager
    registration.register_command("ask-probe", lambda invocation: None, context=True)
    source = _source(profile="alpha")
    current = _entry(source)
    adapter = SimpleNamespace(send=AsyncMock())
    runner = _Runner(current, [current], adapter=adapter)
    invocation = await build_gateway_command_context(
        runner,
        MessageEvent(text="/ask-probe", source=source),
        "ask-probe",
        "",
    )
    assert (
        await invocation.prompt_for_text(
            "What next?", cancel_message="Cancelled this specific command."
        )
    ).ok

    result = await dispatch_pending_plugin_command_followup(
        runner,
        MessageEvent(text="/cancel", source=source),
        current.session_key,
    )

    assert result == "Cancelled this specific command."


@pytest.mark.asyncio
async def test_failed_followup_delivery_does_not_arm_pending_state(
    registered_manager,
):
    _, registration = registered_manager
    registration.register_command("ask-probe", lambda invocation: None, context=True)
    source = _source(profile="alpha")
    current = _entry(source)
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=False, error="offline"))
    )
    runner = _Runner(current, [current], adapter=adapter)
    invocation = await build_gateway_command_context(
        runner,
        MessageEvent(text="/ask-probe", source=source),
        "ask-probe",
        "",
    )

    result = await invocation.prompt_for_text("What next?")

    assert not result.ok
    assert result.message == "offline"
    assert runner.__dict__.get("_pending_plugin_command_followups", {}) == {}


@pytest.mark.asyncio
async def test_unloaded_plugin_does_not_swallow_pending_followup(
    registered_manager,
):
    manager, registration = registered_manager

    async def handler(invocation):
        return "handled"

    registration.register_command("ask-probe", handler, context=True)
    source = _source(profile="alpha")
    current = _entry(source)
    adapter = SimpleNamespace(send=AsyncMock())
    runner = _Runner(current, [current], adapter=adapter)
    command_event = MessageEvent(text="/ask-probe", source=source)
    invocation = await build_gateway_command_context(
        runner, command_event, "ask-probe", ""
    )
    assert (await invocation.prompt_for_text("What next?")).ok

    assert manager.unload(registration.manifest)
    reply_event = MessageEvent(text="ordinary user message", source=source)
    result = await dispatch_pending_plugin_command_followup(
        runner, reply_event, current.session_key
    )

    assert result is False
    assert runner.__dict__.get("_pending_plugin_command_followups", {}) == {}


@pytest.mark.asyncio
async def test_retained_context_loses_external_effect_authority_on_unload(
    registered_manager,
):
    manager, registration = registered_manager
    registration.register_command("effect-probe", lambda ctx: None, context=True)
    token = manager._plugin_commands["effect-probe"]
    source = _source()
    create_topic = AsyncMock(return_value="999")
    runner = _Runner(
        _entry(source), [_entry(source)], adapter=SimpleNamespace(create_topic=create_topic)
    )
    invocation = await build_gateway_command_context(
        runner,
        MessageEvent(text="/effect-probe", source=source),
        "effect-probe",
        "",
        registration=token,
    )

    assert manager.unload(registration.manifest)
    with pytest.raises(CommandCapabilityError, match="unloaded or replaced"):
        await invocation.create_thread("must not send")
    create_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_unload_during_prewrite_await_revokes_session_mutation(
    registered_manager,
):
    manager, registration = registered_manager
    registration.register_command("effect-probe", lambda ctx: None, context=True)
    token = manager._plugin_commands["effect-probe"]
    source = _source()
    current = _entry(source)
    runner = _Runner(current, [current])
    invocation = await build_gateway_command_context(
        runner,
        MessageEvent(text="/effect-probe", source=source),
        "effect-probe",
        "",
        registration=token,
    )

    async def get_then_unload(key):
        assert key == current.session_key
        assert manager.unload(registration.manifest)
        return current

    runner.async_session_store.get_session = get_then_unload
    runner.async_session_store.set_session_cwd = AsyncMock()

    with pytest.raises(CommandCapabilityError, match="revoked before the cwd write"):
        await invocation.set_cwd("/must/not/write")
    runner.async_session_store.set_session_cwd.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_followup_never_transfers_to_replacement_registration(
    registered_manager,
):
    manager, registration = registered_manager
    old_seen = []
    new_seen = []
    registration.register_command(
        "ask-probe", lambda ctx: old_seen.append(ctx.raw_args), context=True
    )
    token = manager._plugin_commands["ask-probe"]
    source = _source(profile="alpha")
    current = _entry(source)
    runner = _Runner(current, [current], adapter=SimpleNamespace(send=AsyncMock()))
    invocation = await build_gateway_command_context(
        runner,
        MessageEvent(text="/ask-probe", source=source),
        "ask-probe",
        "",
        registration=token,
    )
    assert (await invocation.prompt_for_text("What next?")).ok

    assert manager.unload(registration.manifest)
    replacement = PluginContext(
        PluginManifest(name="replacement", source="user"), manager
    )
    replacement.register_command(
        "ask-probe", lambda ctx: new_seen.append(ctx.raw_args), context=True
    )
    result = await dispatch_pending_plugin_command_followup(
        runner,
        MessageEvent(text="private followup", source=source),
        current.session_key,
    )

    assert result is False
    assert old_seen == []
    assert new_seen == []
