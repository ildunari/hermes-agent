"""Ownership guard for ``write_runtime_status`` identity stamping.

Any process importing gateway code (platform adapters via
``gateway/platforms/base.py`` connect/disconnect hooks, detached helpers,
dashboards) can call ``write_runtime_status``. Before the guard, every such
caller stamped ``pid``/``argv``/``start_time`` with its own identity on the
read-merge-write, rewriting the live gateway's identity in
``gateway_state.json`` — which then poisoned restart helpers until the
gateway's next turn/transition. These tests pin the contract:

- a non-owner merges only its named platform payload: it never touches the
  identity of a different live PID, the top-level ``updated_at`` freshness
  signal, or any gateway-level lifecycle field (``gateway_state``,
  ``active_agents``, ``restart_requested``, ``served_profiles``) — all of
  whose legitimate writers run inside the gateway process;
- the owner (pidfile/lock holder) stamps exactly as before;
- a dead recorded identity is safe for any caller to re-stamp.
"""

import json
import os

from gateway import status


_FOREIGN_PID = 424242
_FOREIGN_START_TIME = 111111


def _write_foreign_pid_file(home):
    (home / "gateway.pid").write_text(
        json.dumps(
            {
                "pid": _FOREIGN_PID,
                "kind": "hermes-gateway",
                "argv": ["hermes", "gateway", "run"],
                "start_time": _FOREIGN_START_TIME,
                "hermes_home": str(home),
            }
        ),
        encoding="utf-8",
    )


def _write_foreign_status_file(home, *, updated_at="2026-01-01T00:00:00+00:00"):
    payload = {
        "pid": _FOREIGN_PID,
        "kind": "hermes-gateway",
        "argv": ["hermes", "gateway", "run"],
        "start_time": _FOREIGN_START_TIME,
        "gateway_state": "running",
        "exit_reason": None,
        "restart_requested": False,
        "active_agents": 0,
        "platforms": {},
        "updated_at": updated_at,
    }
    (home / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _pretend_foreign_pid_is_live(monkeypatch):
    monkeypatch.setattr(status, "_pid_exists", lambda pid: int(pid) == _FOREIGN_PID)
    monkeypatch.setattr(
        status,
        "_get_process_start_time",
        lambda pid: _FOREIGN_START_TIME if int(pid) == _FOREIGN_PID else None,
    )


class TestWriteRuntimeStatusOwnershipGuard:
    def test_targeted_profile_platform_write_is_platform_only(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        profile = tmp_path / "profile"
        root.mkdir()
        profile.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(root))
        status.write_pid_file()
        target = profile / "gateway_state.json"
        target.write_text(
            json.dumps({
                "pid": _FOREIGN_PID,
                "gateway_state": "running",
                "active_agents": 9,
                "platforms": {"telegram": {"state": "failed"}},
            }),
            encoding="utf-8",
        )

        status.write_runtime_status(
            platform="telegram",
            platform_state="connected",
            gateway_state="draining",
            active_agents=4,
            status_path=target,
        )

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["platforms"]["telegram"]["state"] == "connected"
        assert payload["pid"] is None
        assert "gateway_state" not in payload
        assert "active_agents" not in payload
        assert "updated_at" not in payload

    def test_foreign_process_merges_platform_state_without_clobbering_identity(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_foreign_pid_file(tmp_path)
        original = _write_foreign_status_file(tmp_path)
        _pretend_foreign_pid_is_live(monkeypatch)

        status.write_runtime_status(platform="telegram", platform_state="connected")

        payload = json.loads((tmp_path / "gateway_state.json").read_text())
        assert payload["pid"] == _FOREIGN_PID
        assert payload["argv"] == original["argv"]
        assert payload["start_time"] == _FOREIGN_START_TIME
        assert payload["updated_at"] == original["updated_at"]
        assert payload["platforms"]["telegram"]["state"] == "connected"

    def test_foreign_process_cannot_forge_lifecycle_fields(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_foreign_pid_file(tmp_path)
        original = _write_foreign_status_file(tmp_path)
        _pretend_foreign_pid_is_live(monkeypatch)

        status.write_runtime_status(
            gateway_state="draining",
            active_agents=3,
            restart_requested=True,
            served_profiles=["forged"],
        )

        payload = json.loads((tmp_path / "gateway_state.json").read_text())
        assert payload["pid"] == _FOREIGN_PID
        # A forged "draining"/busy state could block or force restarts for a
        # gateway that never wrote it — lifecycle fields are owner-only.
        assert payload["gateway_state"] == original["gateway_state"]
        assert payload["active_agents"] == original["active_agents"]
        assert payload["restart_requested"] == original["restart_requested"]
        assert "served_profiles" not in payload

    def test_owner_process_still_stamps_identity(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        status.write_pid_file()
        # Pre-poison the status file with a foreign identity: the owner's
        # write must self-heal it even without any liveness probing.
        _write_foreign_status_file(tmp_path)

        status.write_runtime_status(gateway_state="running")

        payload = json.loads((tmp_path / "gateway_state.json").read_text())
        assert payload["pid"] == os.getpid()
        assert payload["kind"] == "hermes-gateway"
        assert isinstance(payload["argv"], list) and payload["argv"]
        assert payload["gateway_state"] == "running"
        assert payload["updated_at"] != "2026-01-01T00:00:00+00:00"

    def test_dead_recorded_identity_is_safe_to_restamp(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_foreign_status_file(tmp_path)
        # No pidfile, and the recorded PID is dead → legacy stamping behavior.
        monkeypatch.setattr(status, "_pid_exists", lambda _pid: False)

        status.write_runtime_status(gateway_state="starting")

        payload = json.loads((tmp_path / "gateway_state.json").read_text())
        assert payload["pid"] == os.getpid()
        assert payload["gateway_state"] == "starting"

    def test_non_owner_creating_file_leaves_identity_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_foreign_pid_file(tmp_path)
        _pretend_foreign_pid_is_live(monkeypatch)

        status.write_runtime_status(platform="discord", platform_state="connected")

        payload = json.loads((tmp_path / "gateway_state.json").read_text())
        assert payload["pid"] is None
        assert payload["argv"] is None
        assert payload["start_time"] is None
        assert payload["platforms"]["discord"]["state"] == "connected"
        # A fabricated fresh record (updated_at + default lifecycle) would
        # satisfy liveness checks for a gateway that never wrote it.
        assert "updated_at" not in payload
        assert "gateway_state" not in payload
        assert "active_agents" not in payload

    def test_in_process_lock_handle_counts_as_owner(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # A live foreign pidfile would normally deny ownership, but a held
        # runtime lock handle is definitive proof this process is the gateway.
        _write_foreign_pid_file(tmp_path)
        _pretend_foreign_pid_is_live(monkeypatch)
        monkeypatch.setattr(status, "_gateway_lock_handle", object())

        status.write_runtime_status(gateway_state="running")

        payload = json.loads((tmp_path / "gateway_state.json").read_text())
        assert payload["pid"] == os.getpid()
        assert payload["gateway_state"] == "running"
