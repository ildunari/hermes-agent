from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hermes_update_service.py"
SPEC = importlib.util.spec_from_file_location("hermes_update_service", SCRIPT)
assert SPEC and SPEC.loader
SERVICE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVICE
SPEC.loader.exec_module(SERVICE)


def make_ledger(root: Path, run_id: str) -> None:
    directory = SERVICE.run_dir(root, run_id)
    directory.mkdir(mode=0o700, parents=True)
    SERVICE.atomic_json(
        SERVICE.ledger_path(root, run_id),
        {
            "run_id": run_id,
            "phase": "CREATED",
            "status": "RUNNING",
            "phase_history": [],
        },
    )


def test_nonce_replay_is_rejected(tmp_path: Path) -> None:
    nonce = "a" * 32
    timestamp = int(time.time())

    assert SERVICE.nonce_valid(tmp_path, nonce, timestamp)
    assert not SERVICE.nonce_valid(tmp_path, nonce, timestamp)


def test_transition_rejects_phase_regression_and_terminal_resume(tmp_path: Path) -> None:
    run_id = "20260718T120000Z-aaaaaaaaaaaa"
    make_ledger(tmp_path, run_id)

    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    SERVICE.transition(tmp_path, run_id, "MERGED")
    with pytest.raises(RuntimeError, match="phase regression"):
        SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    SERVICE.transition(tmp_path, run_id, "ABORTED")
    with pytest.raises(RuntimeError, match="terminal run"):
        SERVICE.transition(tmp_path, run_id, "VERIFIED")


def test_advance_is_idempotent_after_phase_completed(tmp_path: Path) -> None:
    run_id = "20260718T120000Z-eeeeeeeeeeee"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    SERVICE.transition(tmp_path, run_id, "MERGED")

    ledger = SERVICE.advance(tmp_path, run_id, "PREFLIGHT", bad="must-not-apply")

    assert ledger["phase"] == "MERGED"
    assert "bad" not in ledger


def test_receipt_reconciles_succeeded_action_without_replaying(tmp_path: Path) -> None:
    run_id = "20260718T120000Z-bbbbbbbbbbbb"
    make_ledger(tmp_path, run_id)
    marker = tmp_path / "effect"
    actions = 0

    SERVICE.prepare_receipt(tmp_path, run_id, "stage", "key", {"value": 1})
    marker.write_text("done", encoding="utf-8")

    def probe() -> tuple[bool, dict[str, object]]:
        return marker.is_file(), {"exists": marker.is_file()}

    def action() -> None:
        nonlocal actions
        actions += 1

    receipt = SERVICE.receipted(
        tmp_path,
        run_id,
        "stage",
        "key",
        {"value": 1},
        probe,
        action,
    )

    assert actions == 0
    assert receipt["state"] == "COMPLETED"


def test_receipt_reconciles_irreversible_action_before_abort_check(
    tmp_path: Path,
) -> None:
    run_id = "20260718T120000Z-444444444444"
    make_ledger(tmp_path, run_id)
    marker = tmp_path / "activated"
    marker.write_text("done", encoding="utf-8")
    cancellation_checks = 0

    def probe() -> tuple[bool, dict[str, object]]:
        return marker.is_file(), {"exists": marker.is_file()}

    def cancel() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        raise SERVICE.AbortRequested("late abort")

    receipt = SERVICE.receipted(
        tmp_path,
        run_id,
        "activate",
        "key",
        {"commit": "result"},
        probe,
        lambda: pytest.fail("activation must not replay"),
        cancel,
    )

    assert receipt["state"] == "COMPLETED"
    assert cancellation_checks == 0


def test_bundle_hash_tamper_is_detected(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    bundle = tmp_path / "bundle"
    SERVICE.create_bundle(repo, bundle, SCRIPT)
    SERVICE.verify_bundle(bundle)
    (bundle / "carry.py").chmod(0o700)
    with (bundle / "carry.py").open("a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(RuntimeError, match="hash mismatch"):
        SERVICE.verify_bundle(bundle)


def test_fd_budget_trips_before_more_work(tmp_path: Path) -> None:
    handles = []
    try:
        while SERVICE.fd_count(os.getpid()) <= SERVICE.FD_LIMIT:
            handles.append((tmp_path / f"fd-{len(handles)}").open("w"))
        with pytest.raises(RuntimeError, match="FD budget exceeded"):
            SERVICE.enforce_budget(os.getpid(), None, SERVICE.FD_LIMIT)
    finally:
        for handle in handles:
            handle.close()


def test_child_process_cannot_exceed_fd_limit(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        SERVICE.owned_command(
            [
                sys.executable,
                "-c",
                "handles=[open('/dev/null') for _ in range(300)]",
            ],
            tmp_path,
            tmp_path / "child-fd.log",
            30,
            None,
            SERVICE.FD_LIMIT,
        )


def test_scoped_child_fd_limit_can_be_raised_for_desktop_signing(
    tmp_path: Path,
) -> None:
    result = SERVICE.owned_command(
        [
            sys.executable,
            "-c",
            "handles=[open('/dev/null') for _ in range(300)]",
        ],
        tmp_path,
        tmp_path / "child-fd-raised.log",
        30,
        None,
        SERVICE.FD_LIMIT,
        child_fd_limit=512,
    )
    assert result == 0


def test_abort_terminates_owned_child(tmp_path: Path) -> None:
    abort = tmp_path / "abort.request"
    abort.touch()
    with pytest.raises(SERVICE.AbortRequested):
        SERVICE.owned_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            tmp_path / "abort.log",
            60,
            None,
            SERVICE.FD_LIMIT,
            abort_path=abort,
        )


def test_update_lease_rejects_second_owner(tmp_path: Path) -> None:
    first = SERVICE.acquire_lease(tmp_path, "20260718T120000Z-ffffffffffff")
    try:
        with pytest.raises(RuntimeError, match="lease"):
            SERVICE.acquire_lease(tmp_path, "20260718T120000Z-111111111111")
    finally:
        SERVICE.release_lease(first)


def test_recovery_worker_leaves_ledger_unchanged_when_lease_is_owned(
    tmp_path: Path,
) -> None:
    run_id = "20260718T120000Z-222222222222"
    make_ledger(tmp_path, run_id)
    before = SERVICE.read_json(SERVICE.ledger_path(tmp_path, run_id))
    first = SERVICE.acquire_lease(tmp_path, run_id)
    try:
        SERVICE.execute_worker(tmp_path, tmp_path, run_id)
    finally:
        SERVICE.release_lease(first)

    assert SERVICE.read_json(SERVICE.ledger_path(tmp_path, run_id)) == before


def test_surface_inventory_requires_every_runtime_port(monkeypatch) -> None:
    monkeypatch.setattr(
        SERVICE,
        "listener_pids",
        lambda: {port: port + 10000 for port in SERVICE.REQUIRED_PORTS},
    )
    monkeypatch.setattr(SERVICE, "http_ready", lambda port: port != 9120)

    ready, details = SERVICE.surface_inventory()

    assert not ready
    assert details["http_ready"][9120] is False


def test_desktop_identity_match_is_exact() -> None:
    expected = {
        "CDHash": "abc",
        "TeamIdentifier": SERVICE.EXPECTED_DESKTOP_TEAM,
        "CFBundleVersion": "42",
    }
    assert SERVICE.identity_matches(dict(expected), expected)
    assert not SERVICE.identity_matches(dict(expected, CDHash="different"), expected)


def test_conflict_marker_detection_ignores_separator_text() -> None:
    assert SERVICE.CONFLICT_MARKER_RE.search("<<<<<<< HEAD\nvalue\n=======\n")
    assert not SERVICE.CONFLICT_MARKER_RE.search(
        'expect(label).toBe("=======")\nconst banner = "x<<<<<<< y"\n'
    )


def test_interrupted_desktop_swap_preserves_prior(tmp_path: Path) -> None:
    installed = tmp_path / "Hermes.app"
    prior = tmp_path / "prior.app"
    staging = tmp_path / "staging.app"
    prior.mkdir()
    (prior / "marker").write_text("old", encoding="utf-8")
    staging.mkdir()
    (staging / "marker").write_text("new", encoding="utf-8")

    SERVICE.swap_desktop_apps(staging, installed, prior)

    assert (installed / "marker").read_text(encoding="utf-8") == "new"
    assert (prior / "marker").read_text(encoding="utf-8") == "old"


def test_abort_is_ignored_after_studio_activation(tmp_path: Path) -> None:
    run_id = "20260718T120000Z-333333333333"
    make_ledger(tmp_path, run_id)
    (SERVICE.run_dir(tmp_path, run_id) / "abort.request").touch()
    with pytest.raises(SERVICE.AbortRequested):
        SERVICE.abort_before_activation(tmp_path, run_id)

    SERVICE.transition(tmp_path, run_id, "STUDIO_ACTIVATED")
    SERVICE.abort_before_activation(tmp_path, run_id)


def test_owned_command_can_reconcile_after_activation_abort(tmp_path: Path) -> None:
    abort = tmp_path / "abort.request"
    abort.touch()

    result = SERVICE.owned_command(
        [sys.executable, "-c", "raise SystemExit(0)"],
        tmp_path,
        tmp_path / "reconcile.log",
        30,
        None,
        SERVICE.FD_LIMIT,
        abort_path=None,
    )

    assert result == 0
