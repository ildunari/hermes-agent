from types import SimpleNamespace

from agent.conversation_loop import _accumulate_output_rate
from agent.tool_executor import _record_tool_call_stats


def test_output_rate_accumulator_sums_tokens_and_wall_time():
    agent = SimpleNamespace(session_api_output_tokens=10, session_api_wall_seconds=0.5)
    _accumulate_output_rate(agent, output_tokens=30, api_duration=1.5)
    assert agent.session_api_output_tokens == 40
    assert agent.session_api_wall_seconds == 2.0


def test_output_rate_accumulator_heals_malformed_token_counter():
    agent = SimpleNamespace(
        session_api_output_tokens="bad",
        session_api_wall_seconds=0.5,
    )

    _accumulate_output_rate(agent, output_tokens=30, api_duration=1.5)

    assert agent.session_api_output_tokens == 30
    assert agent.session_api_wall_seconds == 2.0


def test_tool_stats_accumulate_calls_and_errors_per_tool():
    agent = SimpleNamespace(session_tool_stats={})
    _record_tool_call_stats(agent, "terminal", is_error=False)
    _record_tool_call_stats(agent, "terminal", is_error=True)
    _record_tool_call_stats(agent, "delegate_task", is_error=False)
    assert agent.session_tool_stats == {
        "terminal": {"calls": 2, "errors": 1},
        "delegate_task": {"calls": 1, "errors": 0},
    }


def test_tool_stats_heals_malformed_tool_row():
    agent = SimpleNamespace(session_tool_stats={"terminal": None})

    _record_tool_call_stats(agent, "terminal", is_error=True)

    assert agent.session_tool_stats == {
        "terminal": {"calls": 1, "errors": 1},
    }
