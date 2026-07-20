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
        _selected_runtime_identity={"model": "primary", "provider": "openai"},
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_probe_credentials", lambda _agent: None)

    info = server._session_info(agent, session)

    assert info["model"] == "primary"
    assert info["provider"] == "openai"
    assert info["runtime_routing"] == routing


def test_one_turn_restore_owns_top_level_while_last_response_stays_historical(monkeypatch):
    from agent.runtime_routing import emit_runtime_route
    from tui_gateway import server

    agent = SimpleNamespace(
        model="primary", provider="openai", api_key="primary-key", base_url="",
        api_mode="openai_chat_completions", reasoning_config={}, service_tier="",
        session_id="stored", tools=[], event_callback=None, _cached_system_prompt="",
        _primary_runtime=None,
        _selected_runtime_identity={"model": "primary", "provider": "openai"},
        _fallback_activated=False, _fallback_index=0,
    )
    snapshot = server._snapshot_agent_model_runtime(agent)
    agent.model = "one-shot"
    agent.provider = "anthropic"
    agent._selected_runtime_identity = {"model": "one-shot", "provider": "anthropic"}
    one_shot_finished = emit_runtime_route(agent, "finished")

    def switch_model(**kwargs):
        agent.model = kwargs["new_model"]
        agent.provider = kwargs["new_provider"]

    agent.switch_model = switch_model
    server._restore_agent_model_runtime(agent, snapshot)
    agent._runtime_routing = one_shot_finished
    session = {"session_key": "stored", "runtime_routing": one_shot_finished, "running": False}
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_probe_credentials", lambda _agent: None)

    info = server._session_info(agent, session)
    assert (info["model"], info["provider"]) == ("primary", "openai")
    assert info["runtime_routing"] == one_shot_finished
    assert info["runtime_routing"]["runtime"] == {"model": "one-shot", "provider": "anthropic"}

    started = emit_runtime_route(agent, "started")
    assert started["selected"] == {"model": "primary", "provider": "openai"}
    assert started["runtime"] == {"model": "primary", "provider": "openai"}
