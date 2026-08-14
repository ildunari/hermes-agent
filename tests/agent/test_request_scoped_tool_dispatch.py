from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.request_scoped_tools import RequestScopedTool, bind_request_scoped_tools
from agent.tool_executor import execute_tool_calls_sequential


def test_sequential_executor_dispatches_request_scoped_tool():
    agent = MagicMock()
    agent._interrupt_requested = False
    agent._tool_interrupt_requested = False
    agent._incremental_persistence_failed = False
    agent.stream_delta_callback = None
    agent.checkpoints_enabled = False
    agent._tool_approval_manager = None
    agent.session_id = "session-1"
    agent.context_compressor = None
    agent.verbose_logging = False
    agent.tool_progress_callback = None
    agent.tool_complete_callback = None
    agent._should_emit_quiet_tool_messages.return_value = False
    agent._append_incremental_messages.return_value = True
    agent._append_guardrail_observation.side_effect = (
        lambda _name, _args, result, **_kwargs: result
    )
    agent._subdirectory_hints.check_tool_call.return_value = ""
    agent._tool_result_content_for_active_model.side_effect = (
        lambda _name, value: value
    )

    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="contact_memory_search",
            arguments='{"query":"company"}',
        ),
    )
    assistant_message = SimpleNamespace(tool_calls=[tool_call])
    messages = []
    request_tool = RequestScopedTool(
        schema={
            "name": "contact_memory_search",
            "description": "Search trusted contact memory.",
            "parameters": {"type": "object"},
        },
        handler=lambda args: f"found:{args['query']}",
    )

    with bind_request_scoped_tools(agent, [request_tool]):
        execute_tool_calls_sequential(
            agent,
            assistant_message,
            messages,
            effective_task_id="session-1",
        )

    assert messages[-1] == {
        "role": "tool",
        "name": "contact_memory_search",
        "tool_name": "contact_memory_search",
        "content": "found:company",
        "tool_call_id": "call-1",
    }
