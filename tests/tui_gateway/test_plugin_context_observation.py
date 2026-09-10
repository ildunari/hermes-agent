from __future__ import annotations

import json
import threading

from agent.turn_context import (
    _observe_accepted_plugin_context,
    reset_plugin_context_observer,
)
from tui_gateway import server


class _RecordingTransport:
    def __init__(self):
        self.frames = []
        self.lock = threading.Lock()

    def write(self, frame):
        with self.lock:
            self.frames.append(frame)
        return True


def test_duplicate_request_ids_stay_on_their_originating_transport():
    barrier = threading.Barrier(2)
    left = _RecordingTransport()
    right = _RecordingTransport()

    def observe(sid, runtime_session_id, transport, contexts):
        token = server._bind_plugin_context_observation(
            sid, {"prompt_request_id": "same-request", "transport": transport})
        try:
            barrier.wait(timeout=2)
            for context in contexts:
                _observe_accepted_plugin_context(
                    plugin_id="conversation-texture",
                    session_id=runtime_session_id,
                    turn_id=f"turn-{sid}",
                    platform="ios",
                    context=context,
                )
        finally:
            reset_plugin_context_observer(token)

    threads = [
        threading.Thread(
            target=observe,
            args=("ui-left", "runtime-left", left, ("left-private", "left-private-2"))),
        threading.Thread(
            target=observe,
            args=("ui-right", "runtime-right", right, ("right-private",))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    assert len(left.frames) == 2
    assert len(right.frames) == 1
    assert left.frames[0]["params"]["session_id"] == "ui-left"
    assert right.frames[0]["params"]["session_id"] == "ui-right"
    assert left.frames[0]["params"]["payload"]["runtime_session_id"] == "runtime-left"
    assert right.frames[0]["params"]["payload"]["runtime_session_id"] == "runtime-right"
    for transport in (left, right):
        payload = transport.frames[0]["params"]["payload"]
        assert payload["prompt_request_id"] == "same-request"
        assert payload["sequence"] == 1
        assert set(payload) == {
            "schema_version", "prompt_request_id", "sequence", "plugin_id", "hook",
            "platform", "runtime_session_id", "turn_id", "context_sha256",
            "context_length",
        }
    assert [frame["params"]["payload"]["sequence"] for frame in left.frames] == [1, 2]
    serialized = json.dumps(left.frames + right.frames)
    assert "left-private" not in serialized
    assert "right-private" not in serialized


def test_no_bound_observer_emits_no_frame():
    transport = _RecordingTransport()
    _observe_accepted_plugin_context(
        plugin_id="conversation-texture",
        session_id="runtime",
        turn_id="turn",
        platform="ios",
        context="private",
    )
    assert transport.frames == []


def test_only_exact_diagnostics_opt_in_creates_request_binding():
    transport = _RecordingTransport()
    for params in ({}, {"diagnostics": True}, {
        "diagnostics": {"plugin_context_observation": "true"}
    }, {"diagnostics": {"plugin_context_observation": False}}):
        assert server._plugin_context_observation_spec(params, "request", transport) is None

    assert server._plugin_context_observation_spec(
        {"diagnostics": {"plugin_context_observation": True}}, "request", transport
    ) == {"prompt_request_id": "request", "transport": transport}
