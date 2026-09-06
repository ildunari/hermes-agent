"""Process-local admission fence; work remains in the native session owners.

The disk file holds only a restart transaction token, never a session/PID registry.
It intentionally survives ordinary release until support verifies every replacement.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_cli.active_sessions import _FileLock


class MaintenanceConflict(ValueError):
    pass


class OwnerMaintenance:
    def __init__(self, home: Path, kind: str = "serving"):
        self.home = Path(home)
        self.kind = kind
        self.generation = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.path = self.home / "runtime" / "owner-maintenance.json"
        self.file_lock = self.path.with_suffix(".lock")
        self.request_token = self._read_token()
        self.released_request_token = None
        self.stopping = False

    def _read_token(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        token = data.get("request_token")
        if data.get("protocol_version") != 1 or not isinstance(token, str) or not token:
            raise MaintenanceConflict("invalid maintenance bootstrap fence")
        return token

    @property
    def closed(self):
        return self.stopping or self.request_token is not None

    def begin(self, generation: str, token: str):
        with self.lock:
            self._validate(generation, token)
            with _FileLock(self.file_lock):
                held = self._read_token()
                if held not in (None, token) or self.request_token not in (None, token):
                    raise MaintenanceConflict("maintenance owned by another request")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    with tmp.open("x", encoding="utf-8") as stream:
                        json.dump({"protocol_version": 1, "request_token": token}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    tmp.replace(self.path)
                finally:
                    tmp.unlink(missing_ok=True)
            self.request_token = token
            self.released_request_token = None

    def release(self, generation: str, token: str):
        with self.lock:
            self._validate(generation, token)
            if (self.request_token != token and self.released_request_token != token
                    and not (self.request_token is None and self._read_token() == token)):
                raise MaintenanceConflict("maintenance request does not own this owner")
            self.request_token = None
            self.released_request_token = token

    def clear_bootstrap(self, token: str):
        with self.lock, _FileLock(self.file_lock):
            held = self._read_token()
            if held not in (None, token):
                raise MaintenanceConflict("bootstrap owned by another request")
            self.path.unlink(missing_ok=True)

    def _validate(self, generation, token):
        if generation != self.generation:
            raise MaintenanceConflict("stale owner generation")
        if not isinstance(token, str) or not token or len(token) > 256:
            raise MaintenanceConflict("request_token required (1..256 characters)")

    def status(self, sessions=(), *, active_turns=None, queued_turns=0):
        with self.lock:
            active = queued = approvals = 0
            for session in sessions:
                with session["history_lock"]:
                    thread = session.get("_run_thread")
                    active += bool(not session.get("_compute_host_active") and
                                   (session.get("running") or (thread and thread.is_alive())))
                    queued += bool(session.get("queued_prompt")) + len(session.get("queued_prompts") or [])
                    key = session.get("session_key")
                approval = sys.modules.get("tools.approval")
                if approval is not None and key:
                    approvals += len(approval.list_gateway_approvals(key))
            delegates = sys.modules.get("tools.delegate_tool_registry")
            asynchronous = sys.modules.get("tools.async_delegation")
            # Async units may also contain registered children; max is a conservative
            # liveness count without adding two views of the same child together.
            delegation_count = max(
                len(delegates.list_active_subagents()) if delegates else 0,
                asynchronous.active_count() if asynchronous else 0)
            return {
                "owner_pid": os.getpid(), "owner_generation": self.generation,
                "owner_kind": self.kind, "hermes_home": str(self.home),
                "request_token": self.request_token,
                "released_request_token": self.released_request_token,
                "admissions_closed": self.closed,
                "bootstrap_held": self._read_token() is not None,
                "active_turns": active if active_turns is None else max(active, active_turns),
                "queued_turns": queued + queued_turns,
                "delegation_count": delegation_count, "pending_approvals": approvals,
            }


_owner = None
_owner_lock = threading.Lock()


def get_owner(home=None) -> OwnerMaintenance:
    global _owner
    with _owner_lock:
        if _owner is None:
            _owner = OwnerMaintenance(home or get_hermes_home(),
                "compute_host" if os.environ.get("HERMES_COMPUTE_HOST_CHILD") == "1" else "serving")
        return _owner


def local_status():
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return get_owner().status()
    with server._sessions_lock:
        sessions = list(server._sessions.values())
    return get_owner().status(sessions)
