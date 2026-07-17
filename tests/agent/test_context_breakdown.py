"""Tests for live session context breakdown."""

from unittest.mock import MagicMock, patch

import pytest

from agent.context_breakdown import compute_session_context_breakdown


def _make_agent(
    *,
    stable: str = "identity and guidance",
    context: str = "",
    volatile: str = "timestamp line",
    tools: list | None = None,
    context_length: int = 200_000,
    last_prompt_tokens: int = 0,
):
    agent = MagicMock()
    agent.model = "openai/gpt-5.4"
    agent.tools = tools or [
        {"type": "function", "function": {"name": "terminal", "description": "run"}},
        {"type": "function", "function": {"name": "mcp_demo_tool", "description": "mcp"}},
        {"type": "function", "function": {"name": "delegate_task", "description": "spawn"}},
    ]
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = True
    agent.context_compressor = MagicMock(
        context_length=context_length,
        last_prompt_tokens=last_prompt_tokens,
    )
    return agent, {"stable": stable, "context": context, "volatile": volatile}


def test_breakdown_includes_major_categories():
    stable = (
        "base guidance\n"
        "<available_skills>\n  demo:\n    - hello: hi\n</available_skills>"
    )
    context = "# Project Context\nFollow AGENTS.md"
    volatile = "Current time: now"
    history = [{"role": "user", "content": "hello there"}]
    agent, parts = _make_agent(stable=stable, context=context, volatile=volatile)

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, history)

    ids = {item["id"] for item in data["categories"]}
    assert {"system_prompt", "tool_definitions", "rules", "skills", "mcp", "subagent_definitions", "conversation"} <= ids
    assert data["context_max"] == 200_000
    assert data["estimated_total"] > 0


def test_breakdown_uses_measured_context_when_available():
    agent, parts = _make_agent(last_prompt_tokens=42_000)

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, [])

    assert data["context_used"] == 42_000
    assert data["context_percent"] == 21


@pytest.mark.parametrize("auto_mode", ["native", "off", "hermes"])
def test_breakdown_hides_false_categories_for_codex_thread(auto_mode):
    agent, parts = _make_agent(last_prompt_tokens=49_300, context_length=272_000)
    agent.api_mode = "codex_app_server"
    agent.codex_app_server_auto_compaction = auto_mode

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(
            agent,
            [{"role": "user", "content": "local transcript is not the Codex thread"}],
        )

    assert data["context_used"] == 49_300
    assert data["context_percent"] == 18
    assert data["categories"] == []
    assert data["estimated_total"] > 0


@pytest.mark.parametrize("last_prompt_tokens", [0, -1])
def test_breakdown_keeps_unknown_codex_occupancy_unknown(last_prompt_tokens):
    agent, parts = _make_agent(
        last_prompt_tokens=last_prompt_tokens, context_length=272_000
    )
    agent.api_mode = "codex_app_server"

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(
            agent, [{"role": "user", "content": "hello"}]
        )

    assert data["context_used"] == 0
    assert data["context_percent"] == 0
    assert data["categories"] == []
