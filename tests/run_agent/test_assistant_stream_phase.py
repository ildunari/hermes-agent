"""Provider-neutral assistant stream phase contract tests."""

import threading
from types import SimpleNamespace

from agent.codex_runtime import _consume_codex_event_stream
from run_agent import AIAgent


class _Events:
    def __init__(self, events):
        self._events = events

    def __iter__(self):
        return iter(self._events)


def _agent(*, callback=None):
    agent = AIAgent.__new__(AIAgent)
    agent.stream_delta_callback = callback
    agent._stream_callback = None
    agent.reasoning_callback = None
    agent._stream_think_scrubber = None
    agent._stream_context_scrubber = None
    agent._current_streamed_assistant_text = ""
    agent._stream_needs_break = False
    agent._stream_writer_tls = None
    agent._stream_writer_token = 0
    agent._stream_writer_dropped = 0
    agent.session_id = ""
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.platform = "test"
    return agent


def _message_item(phase, text):
    return SimpleNamespace(
        type="message",
        phase=phase,
        status="completed",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def test_generic_delta_remains_unphased_for_phase_aware_callback():
    observed = []
    agent = _agent(callback=lambda text, *, phase=None: observed.append((text, phase)))

    agent._fire_stream_delta("working")

    assert observed == [("working", None)]


def test_final_answer_phase_reaches_new_callback_and_legacy_callback_still_runs():
    semantic = []
    legacy = []
    agent = _agent(callback=lambda text, *, phase=None: semantic.append((text, phase)))
    agent._stream_callback = legacy.append

    agent._fire_stream_delta("answer", phase="final_answer")

    assert semantic == [("answer", "final_answer")]
    assert legacy == ["answer"]


def test_commentary_phase_only_reaches_phase_aware_callbacks():
    semantic = []
    legacy = []
    agent = _agent(callback=lambda text, *, phase=None: semantic.append((text, phase)))
    agent._stream_callback = legacy.append

    agent._fire_stream_delta("checking", phase="commentary")

    assert semantic == [("checking", "commentary")]
    assert legacy == []


def test_phase_aware_callback_error_is_non_fatal():
    def broken(_text, *, phase=None):
        raise RuntimeError(phase)

    agent = _agent(callback=broken)

    agent._fire_stream_delta("answer", phase="final_answer")


def test_superseded_writer_fences_semantic_phase_and_text():
    observed = []
    agent = _agent(
        callback=lambda text, *, phase=None: observed.append((text, phase))
    )
    old_claimed = threading.Event()
    superseded = threading.Event()

    def old_writer():
        agent._claim_stream_writer()
        old_claimed.set()
        superseded.wait(timeout=2)
        agent._fire_stream_delta("stale", phase="final_answer")

    def new_writer():
        old_claimed.wait(timeout=2)
        agent._claim_stream_writer()
        superseded.set()
        agent._fire_stream_delta("current", phase="final_answer")

    first = threading.Thread(target=old_writer)
    second = threading.Thread(target=new_writer)
    first.start()
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert observed == [("current", "final_answer")]
    assert agent._current_streamed_assistant_text == "current"


def test_codex_structured_message_phases_emit_only_evidence_backed_semantics():
    semantic = []
    generic = []
    commentary = _message_item("commentary", "checking")
    final = _message_item("final_answer", "done")

    response = _consume_codex_event_stream(
        _Events(
            [
                SimpleNamespace(
                    type="response.output_item.added",
                    item=SimpleNamespace(type="message", phase="commentary"),
                ),
                SimpleNamespace(type="response.output_text.delta", delta="checking"),
                SimpleNamespace(type="response.output_item.done", item=commentary),
                SimpleNamespace(
                    type="response.output_item.added",
                    item=SimpleNamespace(type="message", phase="final_answer"),
                ),
                SimpleNamespace(type="response.output_text.delta", delta="done"),
                SimpleNamespace(type="response.output_item.done", item=final),
                SimpleNamespace(
                    type="response.output_item.added",
                    item=SimpleNamespace(type="message"),
                ),
                SimpleNamespace(type="response.output_text.delta", delta="unphased"),
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed"),
                ),
            ]
        ),
        model="test-model",
        on_text_delta=generic.append,
        on_semantic_text_delta=lambda text, phase: semantic.append((text, phase)),
    )

    assert semantic == [
        ("checking", "commentary"),
        ("done", "final_answer"),
    ]
    assert generic == ["unphased"]
    assert response.output == [commentary, final]
    assert response.output_text == "doneunphased"


def test_completed_streamed_commentary_is_reconciled_without_duplicate_projection():
    streamed = []
    interim = []
    agent = _agent(
        callback=lambda text, *, phase=None: streamed.append((text, phase))
    )
    agent.interim_assistant_callback = (
        lambda text, *, already_streamed=False: interim.append(
            (text, already_streamed)
        )
    )
    agent._delivered_interim_texts = set()

    agent._fire_stream_delta("checking", phase="commentary")
    agent._fire_streamed_codex_commentary("checking")

    assert streamed == [("checking", "commentary")]
    assert interim == [("checking", True)]


def test_each_completed_semantic_commentary_item_is_marked_already_streamed():
    streamed = []
    interim = []
    agent = _agent(
        callback=lambda text, *, phase=None: streamed.append((text, phase))
    )
    agent.interim_assistant_callback = (
        lambda text, *, already_streamed=False: interim.append(
            (text, already_streamed)
        )
    )
    agent._delivered_interim_texts = set()
    first = _message_item("commentary", "First progress.")
    second = _message_item("commentary", "Second progress.")

    def on_commentary(text, *, already_streamed=False):
        agent._fire_streamed_codex_commentary(
            text,
            already_streamed=already_streamed,
        )

    _consume_codex_event_stream(
        _Events(
            [
                SimpleNamespace(
                    type="response.output_item.added",
                    item=SimpleNamespace(type="message", phase="commentary"),
                ),
                SimpleNamespace(
                    type="response.output_text.delta",
                    delta="First progress.",
                ),
                SimpleNamespace(type="response.output_item.done", item=first),
                SimpleNamespace(
                    type="response.output_item.added",
                    item=SimpleNamespace(type="message", phase="commentary"),
                ),
                SimpleNamespace(
                    type="response.output_text.delta",
                    delta="Second progress.",
                ),
                SimpleNamespace(type="response.output_item.done", item=second),
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed"),
                ),
            ]
        ),
        model="test-model",
        on_semantic_text_delta=(
            lambda text, phase: agent._fire_stream_delta(text, phase=phase)
        ),
        on_commentary_message=on_commentary,
    )

    assert streamed == [
        ("First progress.", "commentary"),
        ("Second progress.", "commentary"),
    ]
    assert interim == [
        ("First progress.", True),
        ("Second progress.", True),
    ]


def test_unknown_codex_message_phase_stays_unphased():
    semantic = []
    generic = []

    _consume_codex_event_stream(
        _Events(
            [
                SimpleNamespace(
                    type="response.output_item.added",
                    item=SimpleNamespace(type="message", phase="future_phase"),
                ),
                SimpleNamespace(type="response.output_text.delta", delta="text"),
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed"),
                ),
            ]
        ),
        model="test-model",
        on_text_delta=generic.append,
        on_semantic_text_delta=lambda text, phase: semantic.append((text, phase)),
    )

    assert semantic == []
    assert generic == ["text"]
