import json
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from gateway.config import Platform


@pytest.mark.asyncio
async def test_non_poke_runner_cannot_start_proactive_watcher(monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "guest")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    await GatewayRunner._proactive_scheduler_watcher(runner)
    assert runner._running is True


def test_runtime_model_calls_are_exact_and_nonfallback(monkeypatch):
    seen = []

    def fake_call(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"allow": false, "reason": "test"}'))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call)
    assert GatewayRunner._proactive_model_text(task="proactive_gate", prompt="x", effort="medium")
    assert seen == [{
        "task": "proactive_gate", "provider": "openai-codex", "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "x"}], "max_tokens": 500,
        "request_overrides": {"reasoning_effort": "medium"}, "allow_fallback": False,
    }]


@pytest.mark.asyncio
async def test_poke_watcher_runs_real_loop_records_health_and_wires_web(monkeypatch, tmp_path):
    lane = {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium", "fallback": False}
    raw = {"auxiliary": {"proactive_gate": lane, "proactive_semantic": lane}, "agent": {"proactive": {
        "enabled": True, "mode": "observe", "dry_run": True,
        "allowed_contacts": [
            {"profile": "poke", "contact_id": "kosta-owner", "principal": "owner"},
            {"profile": "guest", "contact_id": "stephen-lucier", "principal": "guest"},
        ],
        "compose_model": {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "low", "fallback": False},
    }}}
    profile_home = tmp_path / "profiles" / "poke"
    seen = {}
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "poke")
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda profile: profile_home if profile == "poke" else tmp_path / "profiles" / profile)
    monkeypatch.setattr("gateway.run._load_gateway_config_for_profile", lambda profile: raw if profile == "poke" else {})
    monkeypatch.setattr("tools.web_tools.web_search_tool", lambda topic, limit: [{"title": topic, "limit": limit}])

    def tick(**kwargs):
        material = kwargs["web_fallback"].search("ordinary web")
        seen["web"] = material.payload
        return {"armed": 0, "fired": 0, "ignored": 0}

    monkeypatch.setattr("gateway.run._run_proactive_tick_once", tick)
    registry_path = tmp_path / "contacts.json"
    registry_path.write_text(json.dumps({
        "owner_profile": "poke", "owner_contact_id": "kosta-owner",
        "guest_profile": "guest", "owner_identities": ["owner@example.com"],
        "contacts": {"stephen-lucier": {
            "identities": {"bluebubbles": {"handles": ["stephen@example.com"]}},
            "allowed_surfaces": ["bluebubbles"],
        }},
    }))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.BLUEBUBBLES: SimpleNamespace(is_connected=True)}
    runner.config = SimpleNamespace(platforms={
        Platform.BLUEBUBBLES: SimpleNamespace(extra={"guest_contacts_file": str(registry_path)}),
    })

    async def stop_after_tick(_seconds):
        runner._running = False

    monkeypatch.setattr("asyncio.sleep", stop_after_tick)
    await GatewayRunner._proactive_scheduler_watcher(runner)
    assert seen["web"] == [{"title": "ordinary web", "limit": 5}]

    import sqlite3
    con = sqlite3.connect(profile_home / "state.db")
    value = json.loads(con.execute("SELECT value_json FROM proactive_health WHERE key='watcher'").fetchone()[0])
    con.close()
    assert value["completed"] is True and value["adapter_ready"] is True
    assert value["participant_registry_ready"] is True
    assert value["extraction"]["dead_workers"] == 0
