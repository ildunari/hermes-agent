from gateway.usage_render import build_usage_card_args, render_usage_markdown


def _snapshot(**overrides):
    snapshot = {
        "model": "anthropic/claude-sonnet-4.6",
        "provider": "openrouter",
        "profile": "coding",
        "context_used": 81_000,
        "context_length": 130_000,
        "input_tokens": 123_456,
        "output_tokens": 23_456,
        "total_tokens": 146_912,
        "cache_read_tokens": 90_000,
        "prompt_tokens": 120_000,
        "api_calls": 12,
        "avg_output_tokens_per_second": 41.25,
        "duration_seconds": 3_725,
        "tool_stats": {
            "terminal": {"calls": 9, "errors": 1},
            "web_search": {"calls": 7, "errors": 0},
            "delegate_task": {"calls": 4, "errors": 1},
            "read_file": {"calls": 3, "errors": 0},
            "patch": {"calls": 2, "errors": 0},
            "todo": {"calls": 1, "errors": 0},
            "browser_click": {"calls": 1, "errors": 1},
        },
        "subagent_count": 4,
    }
    snapshot.update(overrides)
    return snapshot


def test_render_usage_markdown_has_gauges_sections_and_truncates_tools():
    rendered = render_usage_markdown(_snapshot(), max_tools=5)

    assert "claude-sonnet-4.6 · openrouter · coding" in rendered
    assert "Context  ▕██████░░░░▏ 62% (81k/130k)" in rendered
    assert "Cache    ▕████████░░▏ 75% (90k/120k)" in rendered
    assert "Tokens in" in rendered and "123,456" in rendered
    assert "Avg output tok/s" in rendered and "41.2" in rendered
    assert "Duration" in rendered and "1h 02m 05s" in rendered
    assert "**Tools**" in rendered
    assert "terminal" in rendered and "1 error" in rendered
    assert "patch" in rendered
    assert "todo" not in rendered
    assert "browser_click" not in rendered
    assert "Subagents" in rendered and "4" in rendered
    assert len(rendered.splitlines()) < 30


def test_render_usage_markdown_handles_empty_denominators():
    rendered = render_usage_markdown(
        _snapshot(context_used=0, context_length=0, cache_read_tokens=0, prompt_tokens=0),
    )

    assert "Context  ▕░░░░░░░░░░▏ 0% (0/0)" in rendered
    assert "Cache    ▕░░░░░░░░░░▏ 0% (0/0)" in rendered


def test_build_usage_card_args_uses_same_snapshot_data():
    args = build_usage_card_args(_snapshot())

    assert args["kind"] == "metric_grid"
    assert args["style"]["look"] == "dashboard"
    labels = {metric["label"] for metric in args["metrics"]}
    assert {"Context", "Cache hit", "Tokens", "API calls", "Output speed", "Duration"} <= labels
    assert any(item["label"] == "terminal" and "1 error" in item["detail"] for item in args["items"])
    assert any(item["label"] == "Subagents" and item["value"] == "4" for item in args["items"])


def test_output_rate_is_na_when_backend_cannot_supply_complete_measurement():
    snapshot = _snapshot(output_rate_available=False)

    rendered = render_usage_markdown(snapshot)
    args = build_usage_card_args(snapshot)

    assert "Avg output tok/s   n/a (backend)" in rendered
    speed = next(metric for metric in args["metrics"] if metric["label"] == "Output speed")
    assert speed["value"] == "n/a (backend)"
