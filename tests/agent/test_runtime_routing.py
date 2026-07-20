from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.error_classifier import FailoverReason
from agent.runtime_routing import build_runtime_route, emit_runtime_route


def _agent(**overrides):
    values = {
        "model": "fallback-model",
        "provider": "fallback-provider",
        "_primary_runtime": {"model": "selected-model", "provider": "selected-provider", "api_key": "secret"},
        "_fallback_activated": True,
        "_fallback_index": 2,
        "event_callback": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_runtime_route_is_structured_and_redacted():
    payload = build_runtime_route(_agent(), "fallback_activated", reason=FailoverReason.auth)

    assert payload == {
        "schema_version": 1,
        "state": "fallback_activated",
        "selected": {"model": "selected-model", "provider": "selected-provider"},
        "runtime": {"model": "fallback-model", "provider": "fallback-provider"},
        "fallback": {"active": True, "reason": "authentication", "chain_index": 1},
    }
    assert "secret" not in repr(payload)
    assert "base_url" not in repr(payload)


def test_emit_preserves_reason_for_finished_and_is_callback_safe():
    events = []
    agent = _agent(event_callback=lambda name, payload: events.append((name, payload)))

    emit_runtime_route(agent, "fallback_activated", reason=FailoverReason.rate_limit)
    finished = emit_runtime_route(agent, "finished")

    assert [name for name, _ in events] == ["runtime:route", "runtime:route"]
    assert finished["fallback"]["reason"] == "rate_limit"
    assert agent._runtime_routing is finished


def test_primary_route_uses_live_identity_without_snapshot():
    payload = build_runtime_route(
        _agent(model="chosen", provider="openai", _primary_runtime=None, _fallback_activated=False, _fallback_index=0),
        "started",
    )
    assert payload["selected"] == payload["runtime"] == {"model": "chosen", "provider": "openai"}
    assert payload["fallback"]["active"] is False


def test_selected_identity_is_independent_from_init_fallback_snapshot():
    payload = build_runtime_route(
        _agent(
            _selected_runtime_identity={"model": "requested", "provider": "requested-provider"},
            _primary_runtime={"model": "fallback-model", "provider": "fallback-provider"},
        ),
        "started",
    )
    assert payload["selected"] == {"model": "requested", "provider": "requested-provider"}
    assert payload["runtime"] == {"model": "fallback-model", "provider": "fallback-provider"}


def test_real_init_time_credential_fallback_preserves_requested_identity():
    from run_agent import AIAgent

    fallback_client = MagicMock()
    fallback_client.api_key = "fallback-key"
    fallback_client.base_url = "https://fallback.example/v1"
    fallback_client.default_headers = {}

    def resolve(provider, **_kwargs):
        return (fallback_client, "resolved-backup") if provider == "openai" else (None, None)

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
    ):
        agent = AIAgent(
            model="wanted-model",
            provider="missing-provider",
            api_key="",
            base_url="",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model={"provider": "openai", "model": "backup-model"},
        )

    route = build_runtime_route(agent, "started")
    assert getattr(agent, "_primary_runtime")["model"] == "resolved-backup"  # operational restore remains viable
    assert route["selected"] == {"model": "wanted-model", "provider": "missing-provider"}
    assert route["runtime"] == {"model": "resolved-backup", "provider": "openai"}
    assert route["fallback"]["active"] is True
