"""Hot reload respects engine-owned policy and retries unsuccessful adoption."""

from types import SimpleNamespace

import pytest

from agent.context_engine import ContextEngine
from tui_gateway import server


class StrictEngine(ContextEngine):
    name = "third-party"

    def __init__(self):
        self.calls = []
        self.context_length = 200_000
        self.threshold_tokens = 20_000
        self.threshold_percent = 0.10
        self.protect_last_n = 4

    def __setattr__(self, key, value):
        if key not in {"calls", "context_length", "threshold_tokens", "threshold_percent", "protect_last_n"}:
            raise AttributeError(f"Engine does not expose {key}")
        super().__setattr__(key, value)

    def update_model(self, model, context_length, base_url="", api_key="", provider="", api_mode=""):
        self.calls.append((model, context_length, base_url, api_key, provider, api_mode))
        self.context_length = context_length
        self.threshold_tokens = int(context_length * self.threshold_percent)

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return (prompt_tokens or 0) >= self.threshold_tokens

    def compress(self, messages):
        return messages


class LegacyEngine(StrictEngine):
    def update_model(self, model, context_length, base_url="", api_key=""):
        super().update_model(model, context_length, base_url, api_key)


def _session(engine):
    return {"agent": SimpleNamespace(
        context_compressor=engine, model="runtime-model", provider="runtime-provider",
        base_url="https://runtime.invalid/v1", api_key="test-key", api_mode="responses",
    )}


@pytest.mark.parametrize("engine_type", [StrictEngine, LegacyEngine])
def test_plugin_policy_survives_reload_and_context_unset(monkeypatch, engine_type):
    engine = engine_type()
    session = _session(engine)
    cfg = {"model": {"context_length": 300_000}, "compression": {
        "enabled": False, "codex_responses_native": True,
        "codex_responses_compact_threshold": 123_000, "idle_compact_after_seconds": 45,
        "threshold": 0.95, "threshold_tokens": 290_000, "protect_last_n": 20,
        "model_thresholds": {"runtime-model": 0.99}, "tail_mode": "legacy",
    }}
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    server._sync_agent_compression_with_config("plugin", session)
    expected = ("runtime-model", 300_000, "https://runtime.invalid/v1", "test-key")
    assert engine.calls == [expected + (("runtime-provider", "responses") if engine_type is StrictEngine else ("", ""))]
    assert engine.protect_last_n == 4
    assert engine.threshold_percent == 0.10
    assert engine.should_compress(31_000)  # Legitimate early compaction remains enabled by the engine.
    agent = session["agent"]
    assert (agent.compression_enabled, agent.codex_responses_native_compaction,
            agent.codex_responses_compact_threshold, agent.compression_idle_compact_after_seconds) == (False, True, 123_000, 45)
    server._sync_agent_compression_with_config("plugin", session)
    assert len(engine.calls) == 1

    def resolve(model, **kwargs):
        assert model == agent.model
        assert kwargs == dict(base_url=agent.base_url, api_key=agent.api_key,
                              provider=agent.provider, config_context_length=None, custom_providers=None)
        return 400_000

    monkeypatch.setattr("agent.model_metadata.get_model_context_length", resolve)
    cfg.clear()
    server._sync_agent_compression_with_config("plugin", session)
    assert engine.context_length == 400_000
    assert engine.threshold_tokens == 40_000
    assert agent.compression_enabled is True


def test_failed_apply_retries_same_config_without_masking_engine_typeerror(monkeypatch):
    class FlakyEngine(StrictEngine):
        def update_model(self, model, context_length, **kwargs):
            if not self.calls:
                self.calls.append("failed")
                raise TypeError("engine implementation failed")
            super().update_model(model, context_length, **kwargs)

    engine = FlakyEngine()
    session = _session(engine)
    previous = (("model.context_length", 200_000),)
    session["config_compression_seen"] = previous
    cfg = {"model": {"context_length": 300_000}}
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    server._sync_agent_compression_with_config("retry", session)
    assert session["config_compression_seen"] == previous
    assert engine.calls == ["failed"]
    server._sync_agent_compression_with_config("retry", session)
    assert engine.context_length == 300_000
    assert session["config_compression_seen"] == server._tui_compression_config_signature(cfg)
    assert len(engine.calls) == 2


@pytest.mark.parametrize("model,provider", [
    ("runtime-model", "runtime-provider"), ("gpt-5.5", "openai-codex"),
])
def test_real_lcm_keeps_plugin_settings_on_host_reload(monkeypatch, tmp_path, model, provider):
    # Optional installed plugin, made importable by the test invocation. Never
    # register it with the live plugin loader or write bytecode into its source.
    import sys
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    lcm = pytest.importorskip("hermes_lcm.engine")
    from hermes_lcm.config import LCMConfig

    config = LCMConfig(database_path=str(tmp_path / "lcm.db"),
                       context_threshold=0.10, fresh_tail_count=4,
                       config_sources={"context_threshold": "plugin_config:context_threshold"})
    engine = lcm.LCMEngine(config=config, hermes_home=str(tmp_path))
    try:
        session = _session(engine)
        session["agent"].model = model
        session["agent"].provider = provider
        cfg = {"model": {"context_length": 300_000}, "compression": {
            "threshold": 0.95, "threshold_tokens": 290_000,
            "protect_last_n": 20, "model_thresholds": {model: 0.99},
        }}
        monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
        server._sync_agent_compression_with_config("lcm", session)
        assert engine.raw_context_length == 300_000
        assert 0 < engine.context_length <= engine.raw_context_length
        assert engine.threshold_percent == config.context_threshold
        assert engine.threshold_tokens == int(engine.context_length * config.context_threshold)
        assert not engine.should_compress(engine.threshold_tokens - 1)
        assert engine.should_compress(engine.threshold_tokens + 1)
        assert engine.protect_last_n == config.fresh_tail_count
        assert (engine.model, engine.provider, engine.base_url, engine.api_key, engine.api_mode) == (
            model, provider, "https://runtime.invalid/v1", "test-key", "responses")
        assert session["config_compression_seen"] == server._tui_compression_config_signature(cfg)
    finally:
        engine.shutdown()
