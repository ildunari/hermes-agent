from types import SimpleNamespace

import pytest

from agent import auxiliary_client


class _Completions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )


def _client(completions: _Completions):
    return SimpleNamespace(
        base_url="https://chatgpt.com/backend-api/codex",
        chat=SimpleNamespace(completions=completions),
    )


def test_strict_auxiliary_call_returns_auditable_resolved_route(monkeypatch):
    completions = _Completions()
    monkeypatch.setattr(
        auxiliary_client,
        "_resolve_task_provider_model",
        lambda *args: ("openai-codex", "gpt-5.6-sol", None, None, "codex_responses"),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_get_cached_client",
        lambda *args, **kwargs: (_client(completions), "gpt-5.6-sol"),
    )

    response = auxiliary_client.call_llm(
        task="proactive_gate",
        provider="openai-codex",
        model="gpt-5.6-sol",
        messages=[{"role": "user", "content": "test"}],
        allow_fallback=False,
    )

    assert response._hermes_resolved_route == {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
    }
    assert completions.calls == 1


def test_strict_auxiliary_call_rejects_resolved_model_mismatch(monkeypatch):
    completions = _Completions()
    monkeypatch.setattr(
        auxiliary_client,
        "_resolve_task_provider_model",
        lambda *args: ("openai-codex", "wrong-model", None, None, "codex_responses"),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_get_cached_client",
        lambda *args, **kwargs: (_client(completions), "wrong-model"),
    )

    with pytest.raises(RuntimeError, match="model mismatch"):
        auxiliary_client.call_llm(
            task="proactive_gate",
            provider="openai-codex",
            model="gpt-5.6-sol",
            messages=[{"role": "user", "content": "test"}],
            allow_fallback=False,
        )
    assert completions.calls == 0
