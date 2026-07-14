from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner


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
