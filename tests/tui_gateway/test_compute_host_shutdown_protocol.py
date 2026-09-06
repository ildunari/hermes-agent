"""Exercise the real pipe reader/process exit, with deterministic turn persistence."""
import json
import os
import queue
import signal
import subprocess
import sys
import threading

import pytest


@pytest.mark.parametrize("termination", ["shutdown", "sigterm", "eof"])
def test_protocol_drains_before_exit_and_ack(tmp_path, termination):
    # Stub only the agent/session storage boundary; run_host and its threads are real.
    program = r'''
import sys, types, threading, time
from tui_gateway import compute_host
server = types.ModuleType("tui_gateway.server")
server._sessions = {"s": {}}
def finalize(session, **kwargs):
    print('{"type":"persisted"}', flush=True)
server._finalize_session = finalize
sys.modules["tui_gateway.server"] = server
original = compute_host.ComputeHost.__init__
def init(self, **kwargs):
    original(self, **kwargs)
    def turn():
        time.sleep(.4)
        self.emit({"type":"turn.finished"})
    self._track_turn_future(self._executor.submit(turn), "s")
compute_host.ComputeHost.__init__ = init
compute_host.run_host()
'''
    env = {**os.environ, "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "hermes")}
    proc = subprocess.Popen([sys.executable, "-c", program], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    frames = queue.Queue()
    def read():
        for line in proc.stdout:
            frames.put(json.loads(line))
    threading.Thread(target=read, daemon=True).start()
    try:
        assert frames.get(timeout=10)["type"] == "hello"
        if termination == "shutdown":
            proc.stdin.write('{"type":"shutdown","request_id":"stop"}\n')
            proc.stdin.flush()
        elif termination == "sigterm":
            proc.send_signal(signal.SIGTERM)
        else:
            proc.stdin.close()
        assert frames.get(timeout=5)["type"] == "turn.finished"
        assert frames.get(timeout=5)["type"] == "persisted"
        if termination == "shutdown":
            ack = frames.get(timeout=5)
            assert ack["type"] == "shutdown.ack"
            assert ack["request_id"] == "stop"
        assert proc.wait(timeout=5) == 0
        assert frames.empty(), "shutdown must finalize only once"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
