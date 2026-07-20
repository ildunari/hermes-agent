import asyncio
from types import SimpleNamespace

import pytest

from gateway.platforms.api_server import APIServerAdapter


@pytest.mark.asyncio
async def test_runs_runtime_routing_callback_updates_status_and_stream():
    adapter = object.__new__(APIServerAdapter)
    adapter._run_statuses = {"run_1": {"status": "running"}}
    adapter._run_streams = {"run_1": asyncio.Queue()}
    payload = {
        "schema_version": 1,
        "state": "fallback_activated",
        "selected": {"model": "primary", "provider": "openai"},
        "runtime": {"model": "backup", "provider": "anthropic"},
        "fallback": {"active": True, "reason": "rate_limit", "chain_index": 0},
    }

    adapter._make_run_routing_callback("run_1", asyncio.get_running_loop())("runtime:route", payload)
    await asyncio.sleep(0)

    event = adapter._run_streams["run_1"].get_nowait()
    assert event["event"] == "runtime.routing"
    assert event["routing"] == payload
    assert adapter._run_statuses["run_1"]["runtime_routing"] == payload
    assert adapter._run_statuses["run_1"]["last_event"] == "runtime.routing"


def test_settled_routing_prefers_result_then_agent_then_live_status():
    adapter = object.__new__(APIServerAdapter)
    prior = {"state": "finished"}
    adapter._run_statuses = {"run_1": {"runtime_routing": prior}}
    assert adapter._settled_run_routing("run_1") is prior

    agent_route = {"state": "fallback_activated"}
    agent = SimpleNamespace(_runtime_routing=agent_route)
    assert adapter._settled_run_routing("run_1", agent=agent) is agent_route

    result_route = {"state": "finished", "runtime": {"model": "backup"}}
    assert adapter._settled_run_routing("run_1", agent=agent, result={"runtime_routing": result_route}) is result_route
