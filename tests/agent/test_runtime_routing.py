from types import SimpleNamespace

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
