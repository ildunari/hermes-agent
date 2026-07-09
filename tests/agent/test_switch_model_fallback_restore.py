"""Regression tests for fallback chain and credential-pool restore state.

These cover the Opus/VibeProxy → GPT fallback → switch back to Opus path. The
live bug pruned GPT from the fallback chain because switch_model treated the
turn-scoped fallback provider as the previous primary, leaving DeepSeek as the
next fallback. A later restore also re-selected a stale DeepSeek credential pool
while restoring a VibeProxy primary.
"""

from __future__ import annotations

import types


class _Compressor:
    model = "old"
    base_url = "https://old.example"
    api_key = "old-key"
    provider = "old-provider"
    context_length = 1000
    api_mode = "chat_completions"
    threshold_tokens = 0

    def update_model(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Pool:
    def __init__(self, provider: str):
        self.provider = provider
        self.selected = False

    def has_available(self):
        return True

    def select(self):  # pragma: no cover - failure path for the guard test
        self.selected = True
        raise AssertionError("restore_primary_runtime selected a mismatched pool")


def _agent(provider="openai-codex", model="gpt-5.5"):
    agent = types.SimpleNamespace()
    agent.model = model
    agent.provider = provider
    agent.api_mode = "chat_completions"
    agent.api_key = "old-key"
    agent.base_url = "https://old.example/v1"
    agent.client = object()
    agent._anthropic_client = None
    agent._anthropic_api_key = ""
    agent._anthropic_base_url = ""
    agent._is_anthropic_oauth = False
    agent._client_kwargs = {"api_key": "old-key", "base_url": agent.base_url}
    agent._config_context_length = None
    agent._transport_cache = {}
    agent._credential_pool = None
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._fallback_chain = [
        {"provider": "openai-codex", "model": "gpt-5.5"},
        {"provider": "deepseek", "model": "deepseek-v4-pro"},
    ]
    agent._fallback_model = agent._fallback_chain[0]
    agent._primary_runtime = {
        "model": model,
        "provider": provider,
        "base_url": agent.base_url,
        "api_mode": agent.api_mode,
        "api_key": agent.api_key,
        "client_kwargs": dict(agent._client_kwargs),
        "use_prompt_caching": False,
        "use_native_cache_layout": False,
        "compressor_model": "old",
        "compressor_base_url": "https://old.example/v1",
        "compressor_api_key": "old-key",
        "compressor_provider": provider,
        "compressor_context_length": 1000,
        "compressor_api_mode": "chat_completions",
        "compressor_threshold_tokens": 0,
    }
    agent.context_compressor = _Compressor()
    agent._cached_system_prompt = "cached"
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    agent._session_db = None
    agent.session_id = None
    agent._custom_providers = []
    agent.quiet_mode = True

    agent._create_openai_client = lambda *_args, **_kwargs: object()
    agent._anthropic_prompt_cache_policy = lambda **_kwargs: (False, False)
    agent._ensure_lmstudio_runtime_loaded = lambda: None
    agent._is_azure_openai_url = lambda *_args, **_kwargs: False
    agent._swap_credential = lambda *_args, **_kwargs: setattr(agent, "swapped", True)
    return agent


def test_switching_back_from_fallback_keeps_configured_gpt_fallback(monkeypatch):
    from agent import agent_runtime_helpers as arh

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: None)
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 500000,
    )

    # Current runtime is the turn-scoped GPT fallback, but the saved primary is
    # VibeProxy Opus. Switching back to VibeProxy must not prune openai-codex
    # from the fallback chain.
    agent = _agent(provider="openai-codex", model="gpt-5.5")
    agent._fallback_activated = True
    agent._primary_runtime.update(
        {
            "provider": "vibeproxy",
            "model": "claude-opus-4-8",
            "base_url": "http://127.0.0.1:8485/v1",
        }
    )

    arh.switch_model(
        agent,
        new_model="claude-opus-4-8",
        new_provider="vibeproxy",
        api_key="",
        base_url="http://127.0.0.1:8485/v1",
        api_mode="chat_completions",
    )

    assert [entry["provider"] for entry in agent._fallback_chain] == [
        "openai-codex",
        "deepseek",
    ]


def test_switching_to_new_primary_prunes_rejected_old_primary(monkeypatch):
    from agent import agent_runtime_helpers as arh

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: None)
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 200000,
    )

    agent = _agent(provider="openrouter", model="old-model")
    agent._primary_runtime["provider"] = "openrouter"
    agent._fallback_chain = [
        {"provider": "openrouter", "model": "old-model"},
        {"provider": "anthropic", "model": "new-model"},
        {"provider": "deepseek", "model": "deepseek-v4-pro"},
    ]

    arh.switch_model(
        agent,
        new_model="new-model",
        new_provider="anthropic",
        api_key="sk-test",
        base_url="https://example.invalid/v1",
        api_mode="chat_completions",
    )

    assert [entry["provider"] for entry in agent._fallback_chain] == ["deepseek"]


def test_restore_primary_skips_mismatched_stale_pool():
    from agent import agent_runtime_helpers as arh

    agent = _agent(provider="vibeproxy", model="claude-opus-4-8")
    agent._fallback_activated = True
    agent._credential_pool = _Pool("deepseek")
    agent._primary_runtime.update(
        {
            "provider": "vibeproxy",
            "model": "claude-opus-4-8",
            "base_url": "http://127.0.0.1:8485/v1",
            "client_kwargs": {"api_key": "vibe-key", "base_url": "http://127.0.0.1:8485/v1"},
        }
    )

    assert arh.restore_primary_runtime(agent) is True
    assert agent.provider == "vibeproxy"
    assert agent.model == "claude-opus-4-8"
    assert agent._credential_pool.selected is False
    assert not getattr(agent, "swapped", False)
