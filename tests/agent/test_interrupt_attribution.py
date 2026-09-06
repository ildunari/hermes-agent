"""Cancellation diagnostics must explain admission without retaining user content."""
import asyncio
import hashlib
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.interrupt_control import InterruptControlMixin


class Agent(InterruptControlMixin):
    def __init__(self):
        self.session_id = "session-private"
        self._current_api_request_id = "request-private"
        self._turn_liveness_activity_generation = 7
        self._activity_lock = threading.RLock()
        self._pending_redirect_lock = threading.Lock()
        self._pending_redirect = None
        self._interrupt_requested = False
        self._hard_interrupt_requested = threading.Event()
        self._execution_thread_id = None
        self._active_children_lock = threading.Lock()
        self._active_children = []
        self.quiet_mode = True

    def _liveness_activity_lock(self):
        return self._activity_lock


def records(caplog):
    return [r.cancellation for r in caplog.records if hasattr(r, "cancellation")]


@pytest.mark.parametrize("kind", ["soft", "hard", "guarded", "declined", "redirect"])
def test_admission_is_attributed_without_private_payloads(caplog, kind):
    agent = Agent()
    secret = "correction sk-secret https://private.invalid/key"
    agent._active_request_abort = Mock()
    with caplog.at_level(logging.INFO, logger="run_agent"):
        if kind == "redirect":
            agent._model_request_active = threading.Event()
            agent._model_request_active.set()
            accepted = agent.redirect(secret)
        else:
            accepted = agent.interrupt(
                secret, hard_cancel=kind != "soft", tool_reason=secret,
                require_generation={"guarded": 7, "declined": 6}.get(kind),
            )
    assert accepted is (kind != "declined")
    event, = records(caplog)
    assert event["outcome"] == ("declined_generation" if kind == "declined" else "accepted")
    assert event["reason"] == {
        "soft": "soft_interrupt", "hard": "hard_cancel", "guarded": "generation_guarded_interrupt",
        "declined": "generation_guarded_interrupt", "redirect": "redirect",
    }[kind]
    assert event["session_id_sha256"] == hashlib.sha256(agent.session_id.encode()).hexdigest()
    assert event["api_request_id_sha256"] == hashlib.sha256(agent._current_api_request_id.encode()).hexdigest()
    assert event["activity_generation"] == 7
    assert event["source"].endswith(".test_admission_is_attributed_without_private_payloads")
    assert secret not in caplog.text + repr(event)
    assert "session-private" not in repr(event)
    assert "request-private" not in repr(event)
    assert agent._active_request_abort.call_count == int(accepted)
    assert agent._hard_interrupt_requested.is_set() is (kind in {"hard", "guarded"})


def test_progress_during_compression_wait_declines_without_cancelling(caplog):
    agent = Agent()
    agent._active_request_abort = Mock()

    class CommitFence:
        commit_in_flight = True

        def cancel_before_commit(self):
            # Deterministic interleaving: the commit wait allows real progress
            # to invalidate the already-reserved watchdog claim.
            with agent._activity_lock:
                agent._turn_liveness_activity_generation += 1
                agent._turn_liveness_abort_claim = None
            return False

    agent._active_compression_commit_fence = CommitFence()
    with caplog.at_level(logging.INFO, logger="run_agent"):
        assert agent.interrupt(hard_cancel=True, require_generation=7) is False
    assert not agent._interrupt_requested
    assert not agent._hard_interrupt_requested.is_set()
    agent._active_request_abort.assert_not_called()
    event, = records(caplog)
    assert event["outcome"] == "declined_generation"
    assert event["activity_generation"] == 8
    assert event["required_generation"] == 7


def test_partial_stream_async_completion_then_compression_stop(monkeypatch, caplog):
    """Controlled phase sequence, not a reproduction of an unexplained live cancel.

    Real request drivers run on an async host's worker, then a summary call
    receives explicit Stop. No network retries or tool execution are permitted.
    """
    from run_agent import AIAgent
    from agent import auxiliary_client as aux
    from agent.conversation_compression import CompressionCommitFence
    from hermes_constants import PARTIAL_STREAM_STUB_ID
    from tests.run_agent.test_partial_stream_finish_reason import _make_stream_chunk

    agent = AIAgent(api_key="test-key", base_url="https://example.com/v1",
                    model="test/model", quiet_mode=True, skip_context_files=True,
                    skip_memory=True, enabled_toolsets=[])
    agent.api_mode = "chat_completions"
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    calls = []

    def partial():
        yield _make_stream_chunk(content="partial")
        raise RuntimeError("controlled stream end")

    complete = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="complete", tool_calls=None), finish_reason="stop")])

    def create(**kwargs):
        calls.append(kwargs.get("stream", False))
        return partial() if kwargs.get("stream") else complete

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **kw: client)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **kw: None)
    abort = Mock()
    monkeypatch.setattr(agent, "_abort_request_openai_client", abort)
    agent._current_streamed_assistant_text = "partial"
    summaries = []

    def summary(**kwargs):
        summaries.append(kwargs["task"])
        assert aux._aux_interrupt_protected()
        assert not agent._interrupt_requested
        assert not aux._aux_interrupt_cancel_requested()
        agent.hard_interrupt()
        assert aux._aux_interrupt_cancel_requested()
        raise aux.AuxiliaryExplicitCancellation()

    monkeypatch.setattr("agent.context_compressor.call_llm", summary)

    async def sequence():
        first = await asyncio.to_thread(agent._interruptible_streaming_api_call, {})
        assert first.id == PARTIAL_STREAM_STUB_ID
        assert first.choices[0].message.tool_calls is None
        result = await asyncio.to_thread(agent._interruptible_api_call, {})
        assert result is complete
        assert not agent._interrupt_requested
        assert records(caplog) == []
        agent._active_compression_commit_fence = CompressionCommitFence()
        with aux.aux_interrupt_protection(cancel_check=agent._hard_interrupt_requested.is_set):
            with pytest.raises(aux.AuxiliaryExplicitCancellation):
                agent.context_compressor._generate_summary([
                    {"role": "user", "content": "task"},
                    {"role": "assistant", "content": "complete"},
                ])
        with pytest.raises(InterruptedError):
            await asyncio.to_thread(agent._interruptible_streaming_api_call, {})

    with caplog.at_level(logging.INFO, logger="run_agent"):
        asyncio.run(sequence())
    assert calls == [True, False]
    assert summaries == ["compression"]
    assert agent._interrupt_requested
    assert not agent._has_pending_redirect()
    abort.assert_not_called()  # completed requests no longer own sockets to cancel
    event, = records(caplog)
    assert event["reason"] == "hard_cancel"


@pytest.mark.parametrize("identity", [None, object(), "x" * 4097])
def test_unusable_identity_and_broken_log_handler_cannot_block_stop(monkeypatch, identity):
    from agent import interrupt_diagnostics

    agent = Agent()
    agent.session_id = agent._current_api_request_id = identity
    emitted = []

    def broken_handler(message, event, **kwargs):
        emitted.append(event)
        raise RuntimeError("logging sink unavailable")

    monkeypatch.setattr(interrupt_diagnostics.logger, "info", broken_handler)
    agent.hard_interrupt()
    assert agent._interrupt_requested
    assert agent._hard_interrupt_requested.is_set()
    event, = emitted
    assert event["session_id_sha256"] is None
    assert event["api_request_id_sha256"] is None
