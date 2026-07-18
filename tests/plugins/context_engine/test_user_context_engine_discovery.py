"""Tests for standalone user-dir context-engine discovery and config bridging."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

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


def test_engine_construction_scope_serializes_profile_env(monkeypatch):
    monkeypatch.delenv("LCM_CONTEXT_THRESHOLD", raising=False)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    observed: dict[str, str] = {}

    def construct(name: str, value: float, entered: threading.Event, wait=False):
        cfg = {"context": {"lcm": {"context_threshold": value}}}
        with context_engines.context_engine_construction_scope(cfg, "lcm"):
            observed[name] = os.environ["LCM_CONTEXT_THRESHOLD"]
            entered.set()
            if wait:
                assert release_first.wait(timeout=2)

    first = threading.Thread(
        target=construct, args=("first", 0.31, first_entered, True)
    )
    second = threading.Thread(
        target=construct, args=("second", 0.62, second_entered)
    )
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert observed == {"first": "0.31", "second": "0.62"}


def test_loader_surfaces_import_failure_cause(tmp_path, monkeypatch, caplog):
    user_root = tmp_path / "context_engine"
    package = user_root / "broken"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "raise ModuleNotFoundError('missing_lcm_dependency')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(context_engines, "_user_context_engine_plugins_dir", lambda: user_root)

    with pytest.raises(context_engines.ContextEngineLoadError, match="missing_lcm_dependency"):
        context_engines.load_context_engine("broken")

    assert "missing_lcm_dependency" in caplog.text
