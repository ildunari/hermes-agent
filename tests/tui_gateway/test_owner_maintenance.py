import io
import json
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import owner_maintenance as maintenance
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


@pytest.fixture
def owner(tmp_path, monkeypatch):
    owner = maintenance.OwnerMaintenance(tmp_path)
    monkeypatch.setattr(maintenance, "_owner", owner)
    monkeypatch.setattr(server, "_sessions", {})
    return owner


def test_begin_fences_real_submit_race_and_preserves_control(owner, monkeypatch):
    entered, finish, begun = threading.Event(), threading.Event(), threading.Event()
    results = []
    def submit(rid, params):
        entered.set()
        assert finish.wait(5)
        return {"result": "admitted"}
    monkeypatch.setattr(server, "_submit_admitted_prompt", submit)
    first = threading.Thread(target=lambda: results.append(server.handle_request(
        {"id": 1, "method": "prompt.submit", "params": {"text": "before"}})))
    first.start()
    assert entered.wait(5)
    def begin():
        owner.begin(owner.generation, "operation")
        begun.set()
    waiter = threading.Thread(target=begin)
    waiter.start()
    finish.set()
    first.join(5)
    assert begun.wait(5)
    waiter.join(5)
    assert results == [{"result": "admitted"}]
    assert server.handle_request({"id": 2, "method": "prompt.submit", "params": {}})["error"]["code"] == 5031
    # The real dispatcher still routes Stop and approvals while admission is fenced.
    for method in ("session.interrupt", "approval.respond", "clarify.respond"):
        monkeypatch.setitem(server._methods, method, lambda rid, params: {"result": "control"})
        assert server.handle_request({"id": 3, "method": method}) == {"result": "control"}


def test_compute_admission_counts_native_futures_and_retains_queue(owner, monkeypatch):
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0, max_workers=1)
    entered, finish = threading.Event(), threading.Event()
    def turn(frame):
        entered.set()
        assert finish.wait(5)
    monkeypatch.setattr(host, "_run_real_turn", turn)
    try:
        host.handle_frame({"type": "turn.start", "sid": "one", "request_id": "one"})
        assert entered.wait(5)
        host.handle_frame({"type": "turn.start", "sid": "two", "request_id": "two"})
        host.handle_frame({"type": "maintenance.begin", "request_id": "begin",
                           "owner_generation": owner.generation, "request_token": "operation"})
        state = json.loads(host._stdout.getvalue().splitlines()[-1])["owner"]
        assert state["active_turns"] == 1
        assert state["queued_turns"] == 1
        host.handle_frame({"type": "turn.start", "sid": "three", "request_id": "three"})
        assert json.loads(host._stdout.getvalue().splitlines()[-1])["reason"] == "maintenance"
        session = {"history_lock": threading.Lock(), "running": False,
                   "queued_prompt": {"text": "saved"}, "queued_prompts": [{"text": "next"}]}
        assert not server._drain_queued_prompt("r", "s", session)
        assert session["queued_prompt"]["text"] == "saved"
        assert len(session["queued_prompts"]) == 1
    finally:
        finish.set()
        host._executor.shutdown(wait=True)


def test_generation_token_and_replacement_bootstrap(owner):
    untouched = maintenance.OwnerMaintenance(owner.home)
    owner.begin(owner.generation, "operation")
    owner.begin(owner.generation, "operation")
    replacement = maintenance.OwnerMaintenance(owner.home)
    assert replacement.generation != owner.generation
    assert replacement.closed
    untouched.release(untouched.generation, "operation")
    assert untouched.released_request_token == "operation"
    with pytest.raises(maintenance.MaintenanceConflict):
        replacement.release(owner.generation, "operation")
    with pytest.raises(maintenance.MaintenanceConflict):
        replacement.release(replacement.generation, "wrong")
    replacement.release(replacement.generation, "operation")
    replacement.release(replacement.generation, "operation")
    assert not replacement.closed
    assert replacement.status()["bootstrap_held"]
    assert maintenance.OwnerMaintenance(owner.home).closed
    replacement.clear_bootstrap("operation")
    assert not maintenance.OwnerMaintenance(owner.home).closed
    assert replacement.status()["released_request_token"] == "operation"


def test_disconnected_work_and_approvals_are_not_idle(owner, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "tools.approval", SimpleNamespace(list_gateway_approvals=lambda key: [1]))
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_registry", SimpleNamespace(list_active_subagents=lambda: [1, 2]))
    session = {"session_key": "s", "history_lock": threading.Lock(), "running": True,
               "transport": None, "queued_prompt": {"text": "queued"}}
    owner.begin(owner.generation, "operation")
    state = owner.status([session])
    assert state["active_turns"] == state["pending_approvals"] == state["queued_turns"] == 1
    assert state["delegation_count"] >= 2
    # A native continuation may remain after its original executor future ended.
    monkeypatch.setattr(server, "_sessions", {"disconnected": session})
    finalized = []
    monkeypatch.setattr(server, "_finalize_session", lambda *a, **kw: finalized.append(a))
    ComputeHost(stdout=io.StringIO(), heartbeat_secs=0).flush_all_sessions()
    assert finalized == []


def test_automatic_admission_retains_work_and_existing_reservation(owner, monkeypatch):
    session = {"history_lock": threading.Lock(), "running": False, "agent": object()}
    owner.begin(owner.generation, "operation")
    assert not server._notif_claim_turn(session)
    assert not session["running"]
    assert server._admit_prompt_turn("s", session, "new", None, None) is None
    owner.release(owner.generation, "operation")
    assert server._notif_claim_turn(session)
    owner.begin(owner.generation, "operation")
    monkeypatch.setattr(server, "_admit_unfenced_prompt_turn", lambda *args: ([], session["agent"]))
    assert server._admit_prompt_turn("s", session, "accepted", None, None) == ([], session["agent"])
    assert server._admit_prompt_turn("s", session, "another", None, None) is None


def test_shutdown_cannot_finalize_between_executor_submit_and_tracking(owner, monkeypatch):
    tracking_paused, track_now, shutdown_waiting, finish_turn = (threading.Event() for _ in range(4))
    class ObservedLock:
        def __init__(self):
            self.lock = threading.RLock()
        def __enter__(self):
            if threading.current_thread().name == "shutdown-probe":
                shutdown_waiting.set()
            self.lock.acquire()
        def __exit__(self, *args):
            self.lock.release()
    owner.lock = ObservedLock()
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    original_track = host._track_turn_future
    def paused_track(future, sid):
        tracking_paused.set()
        assert track_now.wait(5)
        original_track(future, sid)
    monkeypatch.setattr(host, "_track_turn_future", paused_track)
    monkeypatch.setattr(host, "_run_real_turn", lambda frame: finish_turn.wait(5))
    skipped = []
    monkeypatch.setattr(host, "flush_all_sessions", lambda **kw: skipped.append(kw["skip_sids"]))
    submit = threading.Thread(target=host.handle_frame,
        args=({"type": "turn.start", "sid": "accepted", "request_id": "r"},))
    shutdown = threading.Thread(name="shutdown-probe", target=host.shutdown,
        kwargs={"reason": "sigterm", "wait": .1})
    try:
        submit.start()
        assert tracking_paused.wait(5)
        shutdown.start()
        assert shutdown_waiting.wait(5)
        track_now.set()
        submit.join(5)
        shutdown.join(5)
        assert not submit.is_alive() and not shutdown.is_alive()
        assert skipped == [{"accepted"}]
        assert host._closed.is_set()
        assert len(host._live_turns()) == 1
    finally:
        track_now.set()
        finish_turn.set()
        submit.join(5)
        if shutdown.ident is not None:
            shutdown.join(5)
        host._executor.shutdown(wait=True)
