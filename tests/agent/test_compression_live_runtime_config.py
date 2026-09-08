"""Rotation must persist current model settings, not constructor defaults."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.conversation_compression import _publish_rotated_compaction
from hermes_state import SessionDB
from tui_gateway.server import _stored_session_runtime_overrides


@pytest.mark.parametrize("service_tier", ["priority", None])
def test_rotation_and_resume_keep_live_reasoning_and_route(tmp_path, service_tier):
    db = SessionDB(db_path=tmp_path / "state.db")
    initial = {"reasoning_config": {"enabled": True, "effort": "low"}, "max_iterations": 250}
    db.create_session(session_id="parent", source="desktop", model="old-model", model_config=initial)
    agent = SimpleNamespace(
        _session_db=db, session_id="parent", platform="desktop", model="selected-model",
        provider="openai-codex", base_url="https://chatgpt.com/backend-api/codex", api_mode="codex_responses",
        reasoning_config={"enabled": True, "effort": "high"}, service_tier=service_tier,
        _session_init_model_config=initial,
        _flush_messages_to_session_db=lambda *args, **kwargs: None,
    )
    lease = SimpleNamespace(holder=None, ttl=300, watermark=None)
    try:
        with patch("agent.conversation_compression._rebind_session_context"), patch("agent.conversation_compression._carry_session_state_to_child"):
            _publish_rotated_compaction(
                agent, [], [{"role": "user", "content": "Preserved task"}],
                new_system_prompt="System", lease=lease, old_session_id="parent",
                compressed_user_turn_outcome="unchanged",
            )
        row = db.get_session(agent.session_id)
        config = json.loads(row["model_config"])
        assert config["reasoning_config"] == agent.reasoning_config
        assert config["provider"] == agent.provider
        assert config["base_url"] == agent.base_url
        assert config["api_mode"] == agent.api_mode
        assert config["max_iterations"] == initial["max_iterations"]
        assert initial["reasoning_config"]["effort"] == "low"
        overrides = _stored_session_runtime_overrides(row)
        assert overrides["reasoning_config_override"] == agent.reasoning_config
        assert overrides["service_tier_override"] == (service_tier or "")
    finally:
        db.close()
