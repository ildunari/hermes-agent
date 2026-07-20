from types import SimpleNamespace


def _routing(state="fallback_activated"):
    return {
        "schema_version": 1,
        "state": state,
        "selected": {"model": "primary", "provider": "openai"},
        "runtime": {"model": "backup", "provider": "anthropic"},
        "fallback": {"active": True, "reason": "rate_limit", "chain_index": 0},
    }


def test_agent_runtime_route_is_session_scoped_and_emitted(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(server, "_sessions", {"sid": {}})
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)))

    server._on_agent_event("sid", "runtime:route", _routing())

    assert server._sessions["sid"]["runtime_routing"]["runtime"]["model"] == "backup"
    assert emitted == [("runtime.route", "sid", _routing())]


def test_session_info_keeps_selected_identity_and_exposes_runtime(monkeypatch):
    from tui_gateway import server

    routing = _routing("finished")
    session = {"session_key": "stored", "runtime_routing": routing, "running": False}
    agent = SimpleNamespace(
        model="backup",
        provider="anthropic",
        reasoning_config={},
        service_tier="",
        session_id="stored",
        tools=[],
        _cached_system_prompt="",
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_probe_credentials", lambda _agent: None)

    info = server._session_info(agent, session)

    assert info["model"] == "primary"
    assert info["provider"] == "openai"
    assert info["runtime_routing"] == routing
