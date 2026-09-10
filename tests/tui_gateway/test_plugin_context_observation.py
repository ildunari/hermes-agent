from __future__ import annotations

import json
import threading

from agent.turn_context import (
    _observe_accepted_plugin_context,
    reset_plugin_context_observer,
)
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


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


def test_opted_in_busy_submit_is_rejected_before_steer_or_queue(monkeypatch):
    """Idle-only contract: a busy opted-in observation must not steal the
    in-flight request id via steer/redirect/queue, including same-socket reuse."""
    sid = "obs-busy-admission"
    session = {
        "history_lock": threading.Lock(),
        "running": True,
        "active_session_lease": object(),
        "title": "busy observation",
        "session_key": "obs-busy-key",
        "attached_images": [],
        "agent": object(),
    }
    busy_calls = []
    real_busy = server._handle_busy_submit

    def _spy_busy(*args, **kwargs):
        busy_calls.append((args, kwargs))
        return real_busy(*args, **kwargs)

    monkeypatch.setattr(server, "_handle_busy_submit", _spy_busy)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    server._sessions[sid] = session
    token = bind_transport(_RecordingTransport())
    try:
        reused_id = "same-request"
        refused = server.handle_request({
            "id": reused_id,
            "method": "prompt.submit",
            "params": {
                "session_id": sid,
                "text": "observe while busy",
                "diagnostics": {"plugin_context_observation": True},
            },
        })
        assert refused["error"]["code"] == 4124
        assert "idle" in refused["error"]["message"]
        assert busy_calls == []
        assert session.get("queued_prompt") is None
        assert not session.get("queued_prompts")
        assert session["running"] is True

        ordinary = server.handle_request({
            "id": reused_id,
            "method": "prompt.submit",
            "params": {
                "session_id": sid,
                "text": "ordinary busy follow-up",
            },
        })
        assert ordinary.get("error") is None
        assert ordinary["result"]["status"] == "queued"
        assert len(busy_calls) == 1
        assert session["queued_prompt"]["text"] == "ordinary busy follow-up"
    finally:
        reset_transport(token)
        server._sessions.pop(sid, None)
