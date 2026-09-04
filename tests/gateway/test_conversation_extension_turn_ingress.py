from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SessionSource
from gateway.run import GatewayRunner


def _runner(tmp_path, order):
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._startup_restore_in_progress = False
    runner._resolve_profile_home_for_source = lambda _source: tmp_path
    runner._admission_scope_for_source = lambda _source, _home: (str(tmp_path), "poke")
    runner._extension_profile_is_ready = lambda _scope: True
    runner._served_profile_names = lambda: ("poke", "guest")
    runner._permitted_extension_routes = lambda: {"poke": ("guest",)}
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized_for_source = lambda _source: order.append("auth") or True
    return runner


def _event():
    event = MessageEvent(
        text="reconnect evidence",
        source=SessionSource(
            platform=Platform.BLUEBUBBLES,
            profile="poke",
            chat_id="iMessage;-;steve@example.com",
            chat_type="dm",
            user_id="steve@example.com",
        ),
        message_id="bb-guid-1",
    )
    event.observed_only = True
    event.communication_ingress = (SimpleNamespace(source_message_id="bb-guid-1"),)
    return event


@pytest.mark.asyncio
async def test_observed_only_reconnect_fires_after_auth_and_never_runs_agent(
    monkeypatch, tmp_path
):
    from gateway import conversation_extension_runtime as runtime

    order = []
    runner = _runner(tmp_path, order)
    context = SimpleNamespace(sender_identity="steve@example.com")
    decision = SimpleNamespace(
        admitted=True,
        reason="",
        runtime_profile="guest",
        extension_id="poke",
        principal="guest",
        subject_id="steve",
    )
    monkeypatch.setattr(runtime, "profile_requirements_satisfied", lambda **_kw: (True, ""))
    monkeypatch.setattr(runtime, "build_route_context", lambda *_a, **_kw: context)
    monkeypatch.setattr(runtime, "admit_and_route", lambda *_a, **_kw: decision)
    monkeypatch.setattr(
        runtime,
        "observe_authenticated_ingress",
        lambda *_a, **_kw: order.append("observe"),
    )
    runner._apply_extension_route_decision_safe = lambda source, event, _decision: (source, event)

    async def must_not_run(**_kwargs):
        pytest.fail("observed-only catch-up must not run an agent turn")

    runner._run_agent_turn_with_policy = must_not_run
    with (
        patch("gateway.run._load_gateway_config_for_profile", return_value={}),
        patch("hermes_cli.lifecycle.invoke_hook", return_value=[]),
    ):
        assert await runner._handle_message(_event()) is None

    assert order == ["auth", "observe"]

