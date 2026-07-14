"""Failing contracts for BlueBubbles guest routing and approval policy."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _source(
    *,
    user_id: str,
    chat_id: str = "iMessage;+;family-chat",
    chat_type: str = "group",
) -> SessionSource:
    return SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name=user_id,
    )


def _event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id="m1")


def _guest_source(**kwargs) -> SessionSource:
    source = _source(**kwargs)
    source.user_id_alt = "guest:steve"
    source.chat_id_alt = "hermes-profile:guest"
    return source


def _runner(*, extra: dict | None = None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.BLUEBUBBLES: PlatformConfig(
                enabled=True,
                token="***",
                extra=extra or {},
            )
        }
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter.resume_typing_for_chat = MagicMock()
    runner.adapters = {Platform.BLUEBUBBLES: adapter}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store._generate_session_key.side_effect = lambda src: build_session_key(src)
    runner.session_store.get_or_create_session.side_effect = lambda src: SessionEntry(
        session_key=build_session_key(src),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=src.platform,
        chat_type=src.chat_type,
        total_tokens=0,
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_sources = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._session_db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


def test_bluebubbles_slash_admin_matches_canonical_contact_identity():
    from gateway.slash_access import policy_for_source

    runner = _runner(
        extra={
            "group_allow_admin_from": ["kosta@example.com"],
            "group_user_allowed_commands": ["help", "whoami"],
        }
    )

    policy = policy_for_source(
        runner.config,
        _source(user_id=" KOSTA@EXAMPLE.COM "),
    )

    assert policy.is_admin(" KOSTA@EXAMPLE.COM ") is True
    assert policy.can_run(" KOSTA@EXAMPLE.COM ", "approve") is True


@pytest.mark.asyncio
async def test_bluebubbles_guest_cannot_approve_owner_bound_session_command():
    runner = _runner(
        extra={
            "group_allow_admin_from": ["kosta@example.com"],
            # Guests may be allowed to use /approve for their own DM approvals,
            # but that must not let them resolve Kosta-bound work in a shared
            # BlueBubbles group session.
            "group_user_allowed_commands": ["approve", "help", "whoami"],
        }
    )
    guest = _source(user_id="guest@example.com")

    with patch("tools.approval.has_blocking_approval", return_value=True), \
            patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
        result = await runner._handle_message(_event("/approve", guest))

    assert resolve.call_count == 0
    assert result is not None
    assert "admin" in result.lower()

@pytest.mark.asyncio
async def test_bluebubbles_guest_cannot_run_admin_slash_commands_by_default():
    runner = _runner(extra={})
    guest = _guest_source(user_id="guest@example.com")

    restart_result = await runner._handle_message(_event("/restart", guest))
    model_result = await runner._handle_message(_event("/model gpt-5.5", guest))

    assert restart_result is not None and "admin-only" in restart_result
    assert model_result is not None and "admin-only" in model_result


@pytest.mark.asyncio
async def test_bluebubbles_guest_default_harmless_slash_command_still_works():
    runner = _runner(extra={})
    guest = _guest_source(user_id="guest@example.com")

    result = await runner._handle_message(_event("/whoami", guest))

    assert result is not None
    assert "Tier: user" in result
    assert "/help" in result
    assert "/status" in result
    assert "/whoami" in result


def test_guest_slash_allowlist_can_add_harmless_command_without_admin():
    from gateway.slash_access import policy_for_source

    runner = _runner(extra={"group_guest_allowed_commands": ["usage"]})
    guest = _guest_source(user_id="guest@example.com")

    policy = policy_for_source(runner.config, guest)

    assert policy.enabled is True
    assert policy.can_run("guest@example.com", "usage") is True
    assert policy.can_run("guest@example.com", "restart") is False


def test_guest_group_allowlist_cannot_enable_admin_commands_in_runner_gate():
    runner = _runner(
        extra={
            "group_guest_allowed_commands": [
                "usage",
                "restart",
                "model",
                "yolo",
            ]
        }
    )
    guest = _guest_source(user_id="guest@example.com")

    assert runner._check_slash_access(guest, "usage") is None
    for command in ("restart", "model", "yolo"):
        denial = runner._check_slash_access(guest, command)
        assert denial is not None
        assert "admin-only" in denial


def test_guest_profile_identity_prompt_loads_configured_prompt_and_soul(tmp_path):
    from gateway.run import _load_guest_profile_identity_prompt

    profile_dir = tmp_path / ".hermes" / "profiles" / "guest"
    profile_dir.mkdir(parents=True)
    (profile_dir / "SOUL.md").write_text("GUEST SOUL SAFETY", encoding="utf-8")
    cfg = {"agent": {"system_prompt": "GUEST CONFIG SAFETY"}}

    with patch("gateway.run.Path.home", return_value=tmp_path):
        prompt = _load_guest_profile_identity_prompt("guest", cfg)

    assert "GUEST CONFIG SAFETY" in prompt
    assert "GUEST SOUL SAFETY" in prompt


def test_guest_agent_ephemeral_context_gets_guest_profile_identity(tmp_path):
    from gateway.run import _prepend_guest_profile_identity_prompt

    profile_dir = tmp_path / ".hermes" / "profiles" / "guest"
    profile_dir.mkdir(parents=True)
    (profile_dir / "SOUL.md").write_text("GUEST SOUL SAFETY", encoding="utf-8")
    guest = _guest_source(user_id="guest@example.com")

    with patch("gateway.run.Path.home", return_value=tmp_path):
        prompt = _prepend_guest_profile_identity_prompt(
            "platform context",
            guest,
            {"agent": {"system_prompt": "GUEST CONFIG SAFETY"}},
            guest_session=True,
        )

    assert "platform context" in prompt
    assert "GUEST CONFIG SAFETY" in prompt
    assert "GUEST SOUL SAFETY" in prompt


def test_bluebubbles_guest_marker_is_authorized_after_registry_classification():
    runner = _runner(extra={})
    guest = _source(user_id="guest@example.com", chat_id="guest@example.com", chat_type="dm")
    guest.user_id_alt = "guest:steve"

    assert runner._is_user_authorized(guest) is True


def test_guest_source_never_uses_generic_agent_proxy():
    from gateway.run import _should_use_agent_proxy

    owner = _source(user_id="kosta@example.com", chat_id="kosta@example.com", chat_type="dm")
    guest = _source(user_id="guest@example.com", chat_id="guest@example.com", chat_type="dm")
    guest.user_id_alt = "guest:steve"

    assert _should_use_agent_proxy("http://127.0.0.1:8000/v1/chat/completions", owner) is True
    assert _should_use_agent_proxy("http://127.0.0.1:8000/v1/chat/completions", guest) is False


def test_guest_runtime_uses_guest_profile_provider_config():
    from gateway.run import _resolve_runtime_agent_kwargs

    guest_config = {
        "model": {
            "default": "guest-model",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        }
    }

    def fake_resolve_runtime_provider(**kwargs):
        assert kwargs["requested"] == "openrouter"
        assert kwargs["explicit_base_url"] == "https://openrouter.ai/api/v1"
        assert kwargs["target_model"] == "guest-model"
        return {
            "provider": "openrouter",
            "api_key": "guest-key",
            "base_url": kwargs["explicit_base_url"],
            "api_mode": "chat_completions",
            "args": [],
        }

    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=fake_resolve_runtime_provider):
        runtime = _resolve_runtime_agent_kwargs(guest_config)

    assert runtime["provider"] == "openrouter"
    assert runtime["api_key"] == "guest-key"
    assert runtime["base_url"] == "https://openrouter.ai/api/v1"


def test_guest_runtime_fallback_uses_supplied_profile_config(tmp_path, monkeypatch):
    from hermes_cli.auth import AuthError
    from gateway.run import _resolve_runtime_agent_kwargs

    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: openai-codex\n"
        "fallback_model:\n  provider: main-fallback\n"
        "  model: main-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)

    guest_config = {
        "model": {"provider": "openai-codex", "default": "guest-primary"},
        "fallback_model": {
            "provider": "guest-fallback",
            "model": "guest-fallback-model",
            "base_url": "https://guest.example/v1",
            "api_key": "guest-fallback-key",
        },
    }
    calls = []

    def fake_resolve_runtime_provider(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise AuthError("guest primary token expired")
        assert kwargs["requested"] == "guest-fallback"
        assert kwargs["explicit_base_url"] == "https://guest.example/v1"
        assert kwargs["explicit_api_key"] == "guest-fallback-key"
        return {
            "provider": "guest-fallback",
            "api_key": kwargs["explicit_api_key"],
            "base_url": kwargs["explicit_base_url"],
            "api_mode": "chat_completions",
            "args": [],
        }

    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=fake_resolve_runtime_provider):
        runtime = _resolve_runtime_agent_kwargs(guest_config)

    assert [call["requested"] for call in calls] == ["openai-codex", "guest-fallback"]
    assert runtime["provider"] == "guest-fallback"
    assert runtime["api_key"] == "guest-fallback-key"
    assert runtime["model"] == "guest-fallback-model"


def test_bluebubbles_guest_group_session_key_is_profile_scoped_from_owner_group_key():
    owner = _source(user_id="kosta@example.com", chat_id="iMessage;+;family-chat", chat_type="group")
    guest = _source(user_id="guest@example.com", chat_id="iMessage;+;family-chat", chat_type="group")
    guest.user_id_alt = "guest:steve"

    assert build_session_key(owner) == "agent:main:bluebubbles:group:iMessage;+;family-chat"
    assert build_session_key(guest) == "agent:guest:bluebubbles:group:iMessage;+;family-chat"


def test_guest_group_approval_requires_prompt_reply_or_explicit_mention():
    from gateway.run import _looks_like_guest_group_approval, _looks_like_guest_group_denial

    assert _looks_like_guest_group_approval("yes", "prompt-1", "prompt-1") is True
    assert _looks_like_guest_group_approval("ok", None, "prompt-1") is False
    assert _looks_like_guest_group_approval("ok", None, "prompt-1", mentioned=True) is True
    assert _looks_like_guest_group_denial("no", "prompt-1", "prompt-1") is True
    assert _looks_like_guest_group_denial("no", None, "prompt-1") is False


@pytest.mark.asyncio
async def test_bluebubbles_live_guest_routing_marks_profile_and_isolated_session(tmp_path):
    registry = tmp_path / "contacts.yaml"
    registry.write_text(
        """
owner_identities:
  - kosta@example.com
guest_profile: guest
contacts:
  stephen-lucier:
    id: stephen-lucier
    display_name: Steve Lucier
    identities:
      bluebubbles:
        handles: [guest@example.com]
    allowed_surfaces: [bluebubbles]
""".strip(),
        encoding="utf-8",
    )
    runner = _runner(extra={"guest_routing_enabled": True, "guest_contacts_file": str(registry)})
    event = _event("hello", _source(user_id="guest@example.com", chat_id="guest@example.com", chat_type="dm"))
    runner._handle_message_with_agent = AsyncMock(return_value=None)

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = await runner._handle_message(event)

    assert result is None
    handler_call = runner._handle_message_with_agent.await_args
    assert handler_call is not None
    captured_event = handler_call.args[0]
    source = captured_event.source
    assert source.profile == "guest"
    assert source.user_id_alt == "guest:stephen-lucier"
    assert source.user_name == "Steve Lucier"
    assert source.chat_id_alt == "hermes-profile:guest"
    assert "Guest contact context" in captured_event.text
    assert "approved_contact_id=stephen-lucier" in captured_event.text
    assert captured_event.metadata["_hermes_contact_scope"] == {
        "principal": "guest", "session_contact_id": "stephen-lucier",
        "source_text": "hello",
    }
    trusted_scope = handler_call.kwargs["trusted_contact_scope"]
    assert trusted_scope.principal == "guest"
    assert trusted_scope.contact_id == "stephen-lucier"
    assert "display_name=Steve Lucier" in captured_event.text
    assert captured_event.text.endswith("hello")


@pytest.mark.asyncio
async def test_bluebubbles_approved_contact_group_gets_no_retrieval_scope(tmp_path):
    registry = tmp_path / "contacts.yaml"
    registry.write_text(
        """
guest_profile: guest
contacts:
  stephen-lucier:
    display_name: Steve Lucier
    identities:
      bluebubbles:
        handles: [guest@example.com]
    allowed_surfaces: [bluebubbles]
""".strip(),
        encoding="utf-8",
    )
    runner = _runner(extra={"guest_routing_enabled": True, "guest_contacts_file": str(registry)})
    event = _event(
        "hello group",
        _source(user_id="guest@example.com", chat_id="iMessage;+;family-chat", chat_type="group"),
    )
    captured = {}

    def stop_after_routing(_hook_name, *, event, gateway, session_store):
        captured["event"] = event
        return [{"action": "skip", "reason": "captured"}]

    with patch("hermes_cli.plugins.invoke_hook", side_effect=stop_after_routing):
        assert await runner._handle_message(event) is None

    assert captured["event"].source.profile == "guest"
    assert captured["event"].source.user_id_alt == "guest:stephen-lucier"
    assert "_hermes_contact_scope" not in captured["event"].metadata


@pytest.mark.asyncio
async def test_bluebubbles_observed_group_message_from_unknown_member_goes_to_guest_context(tmp_path):
    registry = tmp_path / "contacts.yaml"
    registry.write_text(
        """
guest_profile: guest
contacts:
  steve:
    display_name: Steve Lucier
    identities:
      bluebubbles:
        handles: [guest@example.com]
    allowed_surfaces: [bluebubbles]
""".strip(),
        encoding="utf-8",
    )
    runner = _runner(extra={"guest_routing_enabled": True, "guest_contacts_file": str(registry)})
    raw = {"data": {"chats": [{"participants": [{"address": "guest@example.com"}, {"address": "other@example.com"}]}]}}
    event = MessageEvent(
        text="background chatter",
        source=_source(user_id="other@example.com", chat_id="iMessage;+;family-chat", chat_type="group"),
        raw_message=raw,
        message_id="m-observed",
        reply_to_message_id="non-hermes-group-message",
        observed_only=True,
    )

    result = await runner._handle_message(event)

    assert result is None
    cast(Any, runner.adapters[Platform.BLUEBUBBLES]).send.assert_not_called()
    routed_source = runner.session_store.get_or_create_session.call_args.args[0]
    assert routed_source.user_id_alt == "guest:steve"
    assert routed_source.chat_id_alt == "hermes-profile:guest"
    appended = runner.session_store.append_to_transcript.call_args.args[1]
    assert appended["observed"] is True
    assert "background chatter" in appended["content"]
    assert "Guest contact context" not in appended["content"]


@pytest.mark.asyncio
async def test_bluebubbles_unknown_group_ask_prompts_approved_contact_then_approval_runs_original(tmp_path):
    registry = tmp_path / "contacts.yaml"
    registry.write_text(
        """
guest_profile: guest
contacts:
  steve:
    display_name: Steve Lucier
    identities:
      bluebubbles:
        handles: [guest@example.com]
    allowed_surfaces: [bluebubbles]
""".strip(),
        encoding="utf-8",
    )
    runner = _runner(extra={"guest_routing_enabled": True, "guest_contacts_file": str(registry)})
    adapter = cast(Any, runner.adapters[Platform.BLUEBUBBLES])
    adapter.send.return_value = SimpleNamespace(success=True, message_id="prompt-1")
    raw = {"data": {"chats": [{"participants": [{"address": "guest@example.com"}, {"address": "other@example.com"}]}]}}

    unknown = MessageEvent(
        text="what time is dinner?",
        source=_source(user_id="other@example.com", chat_id="iMessage;+;family-chat", chat_type="group"),
        raw_message=raw,
        message_id="m-unknown",
    )
    assert await runner._handle_message(unknown) is None
    adapter.send.assert_called_once()
    assert "approved contacts" in adapter.send.call_args.args[1]

    captured = {}

    def stop_after_routing(_hook_name, *, event, gateway, session_store):
        captured["event"] = event
        return [{"action": "skip", "reason": "captured"}]

    approval = MessageEvent(
        text="yes",
        source=_source(user_id="guest@example.com", chat_id="iMessage;+;family-chat", chat_type="group"),
        raw_message=raw,
        message_id="m-approval",
        reply_to_message_id="prompt-1",
    )
    with patch("hermes_cli.plugins.invoke_hook", side_effect=stop_after_routing):
        assert await runner._handle_message(approval) is None

    assert captured["event"].source.user_id_alt == "guest:approved-group-request:m-unknown"
    assert captured["event"].source.user_name == "other@example.com"
    assert "approved Hermes answering" in captured["event"].text
    assert "authorized Hermes" in captured["event"].text
    assert "message below is from this approved contact" not in captured["event"].text
    assert "what time is dinner?" in captured["event"].text
    assert "_hermes_contact_scope" not in captured["event"].metadata


@pytest.mark.asyncio
async def test_bluebubbles_unknown_group_user_cannot_self_approve_pending_request(tmp_path):
    registry = tmp_path / "contacts.yaml"
    registry.write_text(
        """
guest_profile: guest
contacts:
  steve:
    display_name: Steve Lucier
    identities:
      bluebubbles:
        handles: [guest@example.com]
    allowed_surfaces: [bluebubbles]
""".strip(),
        encoding="utf-8",
    )
    runner = _runner(extra={"guest_routing_enabled": True, "guest_contacts_file": str(registry)})
    adapter = cast(Any, runner.adapters[Platform.BLUEBUBBLES])
    adapter.send.return_value = SimpleNamespace(success=True, message_id="prompt-1")
    raw = {"data": {"chats": [{"participants": [{"address": "guest@example.com"}, {"address": "other@example.com"}]}]}}

    unknown = MessageEvent(
        text="hermes, can you answer this?",
        source=_source(user_id="other@example.com", chat_id="iMessage;+;family-chat", chat_type="group"),
        raw_message=raw,
        message_id="m-unknown",
    )
    assert await runner._handle_message(unknown) is None
    adapter.send.reset_mock()

    self_approval = MessageEvent(
        text="yes",
        source=_source(user_id="other@example.com", chat_id="iMessage;+;family-chat", chat_type="group"),
        raw_message=raw,
        message_id="m-self-approval",
        reply_to_message_id="prompt-1",
    )
    assert await runner._handle_message(self_approval) is None
    adapter.send.assert_not_called()
    cast(Any, runner.session_store.get_or_create_session).assert_not_called()


@pytest.mark.asyncio
async def test_bluebubbles_owner_registry_sender_is_authorized_and_routed(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    registry = tmp_path / "contacts.yaml"
    registry.write_text(
        """
owner_identities:
  - kosta@example.com
owner_profile: poke
owner_contact_id: kosta-owner
guest_profile: guest
contacts:
  steve:
    display_name: Steve Lucier
    identities:
      bluebubbles:
        handles: [guest@example.com]
    allowed_surfaces: [bluebubbles]
""".strip(),
        encoding="utf-8",
    )
    runner = _runner(extra={"guest_routing_enabled": True, "guest_contacts_file": str(registry)})
    runner.adapters[Platform.BLUEBUBBLES].enforces_own_access_policy = False
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner._adapter_enforces_own_access_policy = lambda _platform: False
    runner._is_user_authorized = GatewayRunner._is_user_authorized.__get__(runner, GatewayRunner)
    monkeypatch.delenv("BLUEBUBBLES_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    owner_source = _source(
        user_id="kosta@example.com", chat_id="iMessage;+;family-chat", chat_type="group"
    )
    owner_source.profile = "poke"
    event = MessageEvent(
        text="status",
        source=owner_source,
        raw_message={},
        message_id="m1",
        observed_only=True,
    )

    captured = {}

    def capture_routed_event(_hook_name, *, event, **_kwargs):
        captured["event"] = event
        return []

    with patch("hermes_cli.plugins.invoke_hook", side_effect=capture_routed_event):
        result = await runner._handle_message(event)

    assert result is None
    routed_source = runner.session_store.get_or_create_session.call_args.args[0]
    assert routed_source.profile == "poke"
    assert routed_source.user_id_alt == "owner:poke"
    assert routed_source.chat_id_alt == "hermes-profile:poke"
    assert "_hermes_contact_scope" not in captured["event"].metadata
    assert event.source.profile == "poke"
    runner.session_store.append_to_transcript.assert_called_once()


@pytest.mark.asyncio
async def test_bluebubbles_owner_dm_passes_trusted_contact_scope(tmp_path):
    registry = tmp_path / "contacts.yaml"
    registry.write_text(
        """
owner_identities:
  - kosta@example.com
owner_profile: poke
owner_contact_id: kosta-owner
contacts: {}
""".strip(),
        encoding="utf-8",
    )
    runner = _runner(
        extra={"guest_routing_enabled": True, "guest_contacts_file": str(registry)}
    )
    runner._handle_message_with_agent = AsyncMock(return_value=None)
    event = _event(
        "hello",
        _source(
            user_id="kosta@example.com",
            chat_id="kosta@example.com",
            chat_type="dm",
        ),
    )

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        assert await runner._handle_message(event) is None

    handler_call = runner._handle_message_with_agent.await_args
    assert handler_call is not None
    trusted_scope = handler_call.kwargs["trusted_contact_scope"]
    assert trusted_scope.principal == "owner"
    assert trusted_scope.contact_id == "kosta-owner"


@pytest.mark.asyncio
async def test_bluebubbles_live_guest_routing_denies_unknown_sender(tmp_path):
    registry = tmp_path / "contacts.yaml"
    registry.write_text("owner_identities: []\ncontacts: {}\n", encoding="utf-8")
    runner = _runner(extra={"guest_routing_enabled": True, "guest_contacts_file": str(registry)})
    unknown = _source(user_id="unknown@example.com", chat_id="unknown@example.com", chat_type="dm")

    result = await runner._handle_message(_event("hello", unknown))

    assert result is None
    runner.session_store.get_or_create_session.assert_not_called()


@pytest.mark.asyncio
async def test_bluebubbles_shared_group_approval_fails_closed_without_admin_config():
    runner = _runner(extra={})
    guest = _source(user_id="guest@example.com")

    with patch("tools.approval.has_blocking_approval", return_value=True), \
            patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
        result = await runner._handle_message(_event("/approve", guest))

    assert resolve.call_count == 0
    assert result is not None
    assert "admin-only" in result
