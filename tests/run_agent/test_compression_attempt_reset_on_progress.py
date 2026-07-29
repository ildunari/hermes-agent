"""Productive compressions must not exhaust the per-turn recovery budget.

Live repro (2026-07-27, session 20260727_211255_ee8a35): a long autonomous
turn ran three successful compactions (17:56, 18:03, 18:14 — each committed
real savings), then large tool results regrew the transcript and a 19:03
context-overflow arrived.  The overflow handler found ``compression_attempts``
already at the cap and returned ``compression_exhausted`` WITHOUT attempting
a compression that — judging by the three before it — would have worked.

The cap exists to stop futile thrash (#9893/#35809), so it must bound
CONSECUTIVE unproductive attempts, not total successful compactions per turn.
These tests drive ``run_conversation()`` through more than
``max_compression_attempts`` overflow→compress→retry cycles where every
compression makes real progress, and assert the turn survives.  The
no-progress exhaustion path is asserted unchanged.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def _no_compression_sleep(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_response(content="Hello", finish_reason="stop"):
    msg = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
        reasoning=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model")
    resp.usage = None
    return resp


def _overflow_error():
    err = Exception(
        "Error code: 400 - {'type': 'error', 'error': {'type': "
        "'invalid_request_error', 'message': 'prompt is too long: "
        "233153 tokens > 200000 maximum'}}"
    )
    err.status_code = 400
    return err


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.tool_delay = 0
        a.compression_enabled = True
        a.save_trajectories = False
        return a


def _history(n: int) -> list:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i} " + "x" * 200}
        for i in range(n)
    ]


class TestProductiveCompressionResetsAttemptBudget:
    def test_turn_survives_more_overflow_recoveries_than_the_cap(self, agent):
        """5 overflow→productive-compression cycles in one turn must recover.

        Before the fix the 4th overflow returned ``compression_exhausted``
        even though every prior compression had made real progress.
        """
        assert agent.max_compression_attempts == 3

        n_overflows = 5
        responses = [_overflow_error() for _ in range(n_overflows)]
        responses.append(_mock_response(content="Recovered", finish_reason="stop"))
        agent.client.chat.completions.create.side_effect = responses

        compress_calls = []

        def _productive_compress(messages, system_message, **_kwargs):
            # Drop one message per pass — unambiguous progress
            # (len(after) < len(before)) for _compression_progress.
            compress_calls.append(len(messages))
            return messages[:-1], agent._cached_system_prompt

        with (
            patch.object(agent, "_compress_context", side_effect=_productive_compress),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation(
                "hello", conversation_history=_history(40)
            )

        assert len(compress_calls) == n_overflows, (
            f"every overflow should have been answered with a compression, "
            f"got {len(compress_calls)} for {n_overflows} overflows"
        )
        assert result["completed"] is True, (
            "productive compressions must not exhaust the per-turn budget: "
            f"turn failed with {result.get('error')!r}"
        )
        assert not result.get("compression_exhausted")

    def test_unproductive_compressions_still_exhaust_the_cap(self, agent):
        """No-progress compressions keep consuming the budget and terminate.

        Guards the anti-thrash property the cap was built for: identical
        messages back from every compression (and no image payloads to
        strip) must still end the turn as compression_exhausted after the
        capped number of attempts.
        """
        assert agent.max_compression_attempts == 3

        agent.client.chat.completions.create.side_effect = [
            _overflow_error() for _ in range(10)
        ]

        compress_calls = []

        def _noop_compress(messages, system_message, **_kwargs):
            compress_calls.append(len(messages))
            return messages, agent._cached_system_prompt

        with (
            patch.object(agent, "_compress_context", side_effect=_noop_compress),
            patch.object(
                agent, "_try_strip_image_parts_from_tool_messages", return_value=False
            ),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation(
                "hello", conversation_history=_history(40)
            )

        assert result["completed"] is False
        assert len(compress_calls) <= agent.max_compression_attempts + 1
