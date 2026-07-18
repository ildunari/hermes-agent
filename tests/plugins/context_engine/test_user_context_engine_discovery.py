"""Tests for standalone user-dir context-engine discovery and config bridging."""

from __future__ import annotations

import os
from pathlib import Path

from plugins import context_engine as context_engines


def _write_user_engine(root: Path) -> Path:
    package = root / "hermes-lcm"
    package.mkdir(parents=True)
    (package / "plugin.yaml").write_text(
        "name: hermes-lcm\ndescription: test standalone LCM\n"
    )
    (package / "__init__.py").write_text(
        """
from agent.context_engine import ContextEngine

class TestLCMEngine(ContextEngine):
    name = "lcm"
    def update_from_response(self, usage): pass
    def should_compress(self, prompt_tokens=None): return False
    def compress(self, messages, current_tokens=None, focus_topic=None): return messages
    def get_tool_schemas(self):
        return [{"name": "lcm_grep", "description": "test", "parameters": {"type": "object", "properties": {}}}]

def register(ctx):
    ctx.register_context_engine(TestLCMEngine())
""".lstrip()
    )
    return package


def test_loads_registered_name_from_differently_named_user_package(tmp_path, monkeypatch):
    user_root = tmp_path / "context_engine"
    _write_user_engine(user_root)
    monkeypatch.setattr(context_engines, "_user_context_engine_plugins_dir", lambda: user_root)

    engine = context_engines.load_context_engine("lcm")

    assert engine is not None
    assert engine.name == "lcm"
    assert engine.get_tool_schemas()[0]["name"] == "lcm_grep"


def test_discovery_reports_registered_engine_name_and_metadata(tmp_path, monkeypatch):
    user_root = tmp_path / "context_engine"
    _write_user_engine(user_root)
    empty_repo_root = tmp_path / "repo-engines"
    empty_repo_root.mkdir()
    monkeypatch.setattr(context_engines, "_CONTEXT_ENGINE_PLUGINS_DIR", empty_repo_root)
    monkeypatch.setattr(context_engines, "_user_context_engine_plugins_dir", lambda: user_root)

    assert context_engines.discover_context_engines() == [
        ("lcm", "test standalone LCM", True),
    ]


def test_context_engine_config_bridge_serializes_scalars_and_clears_on_rollback(monkeypatch):
    monkeypatch.delenv("LCM_CONTEXT_THRESHOLD", raising=False)
    monkeypatch.delenv("LCM_DYNAMIC_LEAF_CHUNK_ENABLED", raising=False)
    cfg = {
        "context": {
            "engine": "lcm",
            "lcm": {
                "context_threshold": 0.35,
                "dynamic_leaf_chunk_enabled": True,
            },
        }
    }

    bridged = context_engines.bridge_context_engine_config_to_env(cfg, "lcm")

    assert bridged == {
        "LCM_CONTEXT_THRESHOLD": "0.35",
        "LCM_DYNAMIC_LEAF_CHUNK_ENABLED": "true",
    }
    assert os.environ["LCM_CONTEXT_THRESHOLD"] == "0.35"
    assert os.environ["LCM_DYNAMIC_LEAF_CHUNK_ENABLED"] == "true"

    context_engines.bridge_context_engine_config_to_env(
        {"context": {"engine": "compressor"}},
        "compressor",
    )
    assert "LCM_CONTEXT_THRESHOLD" not in os.environ
    assert "LCM_DYNAMIC_LEAF_CHUNK_ENABLED" not in os.environ


def test_context_engine_config_bridge_restores_external_env_on_rollback(monkeypatch):
    monkeypatch.setenv("LCM_CONTEXT_THRESHOLD", "0.7")

    context_engines.bridge_context_engine_config_to_env(
        {"context": {"lcm": {"context_threshold": 0.35}}},
        "lcm",
    )
    assert os.environ["LCM_CONTEXT_THRESHOLD"] == "0.35"

    context_engines.bridge_context_engine_config_to_env(
        {"context": {"engine": "compressor"}},
        "compressor",
    )
    assert os.environ["LCM_CONTEXT_THRESHOLD"] == "0.7"
