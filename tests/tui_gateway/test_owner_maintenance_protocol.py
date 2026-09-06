"""Use the actual supervisor and child pipe protocol, without model calls/hooks."""
import os
import queue
import sys

from tui_gateway.host_supervisor import HostSupervisor


def test_supervisor_maintenance_is_owner_correlated_and_stop_drains(tmp_path):
    program = r'''
import os, sys, types, threading
from tui_gateway import compute_host
server = types.ModuleType("tui_gateway.server")
done = threading.Event()
server._sessions = {"s": {"session_key":"s", "history_lock":threading.Lock(), "running":False}}
server._sessions_lock = threading.Lock()
server._finalize_session = lambda *a, **k: None
server._interrupt_session_turn = lambda *a: done.set()
sys.modules["tui_gateway.server"] = server
def turn(self, frame):
    server._sessions["s"]["running"] = True
    self.emit({"type":"rpc", "message":{"started":True}})
    done.wait(30)
    server._sessions["s"]["running"] = False
    self.emit({"type":"turn.end", "sid":"s", "request_id":frame["request_id"]})
compute_host.ComputeHost._run_real_turn = turn
os._exit(compute_host.main([]))
'''
    events, ended = queue.Queue(), queue.Queue()
    supervisor = HostSupervisor(
        registry_path=tmp_path / "host.json", argv=[sys.executable, "-c", program],
        env={"HOME": str(tmp_path), "HERMES_HOME": str(tmp_path)},
        expected_hermes_home=str(tmp_path), rpc_sink=events.put)
    try:
        initial = supervisor.maintenance("status")["owner"]
        assert initial["owner_pid"] == supervisor.pid
        assert initial["owner_generation"] == supervisor._hello["boot_id"]
        supervisor.submit_turn({"sid": "s", "request_id": "turn"}, on_complete=ended.put)
        assert events.get(timeout=5) == {"started": True}
        generation = initial["owner_generation"]
        held = supervisor.maintenance("begin", owner_generation=generation, request_token="operation")["owner"]
        assert held["admissions_closed"] and held["bootstrap_held"]
        assert held["active_turns"] == 1
        assert supervisor.maintenance("release", owner_generation="old", request_token="operation")["type"] == "maintenance.error"
        rejected = queue.Queue()
        supervisor.submit_turn({"sid": "s", "request_id": "rejected"}, on_complete=rejected.put)
        assert rejected.get(timeout=5)["reason"] == "maintenance"
        supervisor.interrupt("s")
        assert ended.get(timeout=5)["type"] == "turn.end"
        state = supervisor.maintenance("status")["owner"]
        assert state["active_turns"] == state["queued_turns"] == 0
        released = supervisor.maintenance("release", owner_generation=generation, request_token="operation")["owner"]
        assert not released["admissions_closed"] and released["bootstrap_held"]
    finally:
        supervisor.shutdown()
    assert not supervisor.is_running()


def test_supervisor_shutdown_reserves_flush_and_leaves_over_budget_turn_recoverable(tmp_path):
    program = r'''
import os, sys, types, threading
from pathlib import Path
from tui_gateway import compute_host
home = Path(os.environ["HERMES_HOME"])
server = types.ModuleType("tui_gateway.server")
server._sessions_lock = threading.Lock()
server._sessions = {sid: {"session_key":sid, "history_lock":threading.Lock()} for sid in ("live", "idle")}
def finalize(session, **kwargs):
    (home / (session["session_key"] + ".finalized")).write_text("persisted")
server._finalize_session = finalize
sys.modules["tui_gateway.server"] = server
def turn(self, frame):
    (home / "live.recoverable").write_text("unfinished prompt")
    self.emit({"type":"rpc", "message":{"started":True}})
    threading.Event().wait(60)
compute_host.ComputeHost._run_real_turn = turn
os._exit(compute_host.main([]))
'''
    events = queue.Queue()
    supervisor = HostSupervisor(
        registry_path=tmp_path / "host.json", argv=[sys.executable, "-c", program],
        env={"HOME": str(tmp_path), "HERMES_HOME": str(tmp_path)},
        expected_hermes_home=str(tmp_path), rpc_sink=events.put)
    try:
        supervisor.submit_turn({"sid": "live", "request_id": "long"})
        assert events.get(timeout=5) == {"started": True}
        supervisor.shutdown()
        assert not supervisor.is_running()
        assert supervisor._proc.returncode == 0, "graceful exit, not supervisor SIGKILL"
        assert (tmp_path / "idle.finalized").read_text() == "persisted"
        assert not (tmp_path / "live.finalized").exists()
        assert (tmp_path / "live.recoverable").read_text() == "unfinished prompt"
    finally:
        if supervisor.is_running():
            supervisor.shutdown()
