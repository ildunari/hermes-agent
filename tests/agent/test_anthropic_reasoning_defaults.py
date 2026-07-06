from agent.anthropic_adapter import build_anthropic_kwargs


def test_native_claude_medium_default_promotes_to_high_adaptive_thinking():
    kwargs = build_anthropic_kwargs(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=None,
        reasoning_config={"enabled": True, "effort": "medium"},
    )

    assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert kwargs["output_config"] == {"effort": "high"}


def test_native_claude_disabled_reasoning_stays_disabled():
    kwargs = build_anthropic_kwargs(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=None,
        reasoning_config={"enabled": False},
    )

    assert "thinking" not in kwargs
    assert "output_config" not in kwargs
