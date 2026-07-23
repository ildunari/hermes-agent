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


def test_service_staging_push_bypasses_duplicate_interactive_hooks() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"core.hooksPath=/dev/null"' in source
    assert '"push-run-ref"' in source


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


def test_parked_run_is_active_and_blocks_second_start(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-555555555555"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    ledger = SERVICE.transition(
        tmp_path, run_id, SERVICE.PARKED, conflict_files=["a.py"]
    )

    assert ledger["status"] == "NEEDS_RESOLUTION"
    assert SERVICE.active_run(tmp_path) == run_id


def test_parked_run_resumes_forward_and_can_repark(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-666666666666"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    SERVICE.transition(tmp_path, run_id, SERVICE.PARKED)

    ledger = SERVICE.read_json(SERVICE.ledger_path(tmp_path, run_id))
    assert SERVICE.phase_before(ledger, "MERGED")
    assert not SERVICE.phase_before(ledger, "PREFLIGHT")

    SERVICE.transition(tmp_path, run_id, SERVICE.PARKED)
    SERVICE.transition(tmp_path, run_id, "MERGED")
    with pytest.raises(RuntimeError, match="phase regression"):
        SERVICE.transition(tmp_path, run_id, SERVICE.PARKED)


def test_record_preserves_parked_phase(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-fffffffffff0"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    SERVICE.transition(tmp_path, run_id, SERVICE.PARKED)

    ledger = SERVICE.record(tmp_path, run_id, checkpoint_ref="refs/x")

    assert ledger["phase"] == "NEEDS_RESOLUTION"
    assert ledger["status"] == "NEEDS_RESOLUTION"
    assert ledger["checkpoint_ref"] == "refs/x"


def test_parked_run_can_still_fail_or_abort(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-777777777777"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    SERVICE.transition(tmp_path, run_id, SERVICE.PARKED)
    SERVICE.transition(tmp_path, run_id, "FAILED", error="boom")
    with pytest.raises(RuntimeError, match="terminal run"):
        SERVICE.transition(tmp_path, run_id, "MERGED")


def test_resume_verb_spawns_worker_for_parked_run(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "20260723T120000Z-888888888888"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    SERVICE.transition(tmp_path, run_id, SERVICE.PARKED)
    spawned: list[str] = []
    monkeypatch.setattr(
        SERVICE, "spawn_worker", lambda repo, root, rid: spawned.append(rid)
    )

    payload = {
        "verb": "resume",
        "run_id": run_id,
        "nonce": "c" * 32,
        "timestamp": int(time.time()),
    }
    response = SERVICE.handle_request(tmp_path, tmp_path, payload, None)

    assert response == {"ok": True, "run_id": run_id, "resumed": True}
    assert spawned == [run_id]


def test_resume_verb_rejects_terminal_run(tmp_path: Path, monkeypatch) -> None:
    run_id = "20260723T120000Z-999999999999"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "FAILED", error="boom")
    monkeypatch.setattr(
        SERVICE,
        "spawn_worker",
        lambda repo, root, rid: pytest.fail("must not spawn"),
    )

    payload = {
        "verb": "resume",
        "run_id": run_id,
        "nonce": "d" * 32,
        "timestamp": int(time.time()),
    }
    with pytest.raises(RuntimeError, match="terminal"):
        SERVICE.handle_request(tmp_path, tmp_path, payload, None)


def test_surface_inventory_best_effort_port_does_not_gate(monkeypatch) -> None:
    monkeypatch.setattr(SERVICE, "BEST_EFFORT_PORTS", (9999,))
    monkeypatch.setattr(
        SERVICE,
        "listener_pids",
        lambda: {
            port: port + 10000
            for port in (*SERVICE.REQUIRED_PORTS, *SERVICE.LISTENER_ONLY_PORTS)
        },
    )
    monkeypatch.setattr(SERVICE, "http_ready", lambda port: port != 9999)

    ready, details = SERVICE.surface_inventory()

    assert ready
    assert details["http_ready"][9999] is False


def test_surface_inventory_requires_listener_only_ports_replaced(
    monkeypatch,
) -> None:
    old = {
        port: port + 10000
        for port in (*SERVICE.REQUIRED_PORTS, *SERVICE.LISTENER_ONLY_PORTS)
    }
    stale = dict(old)
    monkeypatch.setattr(SERVICE, "listener_pids", lambda: stale)
    monkeypatch.setattr(SERVICE, "http_ready", lambda port: True)

    ready, details = SERVICE.surface_inventory(old)

    assert not ready
    assert details["replaced"][SERVICE.LISTENER_ONLY_PORTS[0]] is False


def test_resume_verb_tolerates_released_lease_file(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "20260723T120000Z-cccccccccccd"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    SERVICE.transition(tmp_path, run_id, SERVICE.PARKED)
    handle = SERVICE.acquire_lease(tmp_path, run_id)
    SERVICE.release_lease(handle)
    assert (tmp_path / "lease.json").is_file()
    spawned: list[str] = []
    monkeypatch.setattr(
        SERVICE, "spawn_worker", lambda repo, root, rid: spawned.append(rid)
    )

    payload = {
        "verb": "resume",
        "run_id": run_id,
        "nonce": "e" * 32,
        "timestamp": int(time.time()),
    }
    response = SERVICE.handle_request(tmp_path, tmp_path, payload, None)

    assert response["ok"] is True
    assert spawned == [run_id]


def test_abort_verb_finalizes_parked_run_without_worker(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-ddddddddddde"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    SERVICE.transition(tmp_path, run_id, SERVICE.PARKED)

    payload = {
        "verb": "abort",
        "run_id": run_id,
        "nonce": "f" * 32,
        "timestamp": int(time.time()),
    }
    response = SERVICE.handle_request(tmp_path, tmp_path, payload, None)

    assert response.get("aborted") is True
    ledger = SERVICE.read_json(SERVICE.ledger_path(tmp_path, run_id))
    assert ledger["status"] == "ABORTED"
    assert SERVICE.active_run(tmp_path) is None


def test_abort_verb_leaves_marker_for_activated_run(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-eeeeeeeeeeef"
    make_ledger(tmp_path, run_id)
    for phase in (
        "PREFLIGHT",
        "MERGED",
        "VERIFIED",
        "BUILT",
        "MACBOOK_STAGED",
        "STUDIO_ACTIVATED",
    ):
        SERVICE.transition(tmp_path, run_id, phase)

    payload = {
        "verb": "abort",
        "run_id": run_id,
        "nonce": "a1" + "b" * 30,
        "timestamp": int(time.time()),
    }
    response = SERVICE.handle_request(tmp_path, tmp_path, payload, None)

    assert response.get("abort_requested") is True
    ledger = SERVICE.read_json(SERVICE.ledger_path(tmp_path, run_id))
    assert ledger["status"] == "RUNNING"


def test_binary_and_missing_conflicts_are_not_auto_resolved(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "asset.png"
    binary.write_bytes(b"\x89PNG\x00binary")
    text = tmp_path / "resolved.py"
    text.write_text("value = 1\n", encoding="utf-8")
    conflicted = tmp_path / "conflicted.py"
    conflicted.write_text("<<<<<<< HEAD\nvalue\n=======\nother\n", encoding="utf-8")

    assert not SERVICE.conflict_is_resolved(binary)
    assert not SERVICE.conflict_is_resolved(tmp_path / "deleted.py")
    assert not SERVICE.conflict_is_resolved(conflicted)
    assert SERVICE.conflict_is_resolved(text)


def test_wait_for_surfaces_times_out_naming_blocked_ports(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-aaaaaaaaaaab"
    make_ledger(tmp_path, run_id)

    def probe() -> tuple[bool, dict[str, object]]:
        return False, {
            "http_ready": {9119: False, 8642: True},
            "replaced": {9120: False},
        }

    with pytest.raises(RuntimeError, match="9119, 9120"):
        SERVICE.wait_for_surfaces(
            tmp_path,
            run_id,
            probe,
            wait_seconds=0.2,
            poll_seconds=0.05,
            audit_seconds=0,
            sleeper=lambda _: None,
        )

    audit = (
        SERVICE.run_dir(tmp_path, run_id) / "evidence" / "drain-audit.jsonl"
    )
    lines = audit.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    record = json.loads(lines[0])
    assert record["http_ready"]["9119"] is False


def test_failed_after_activation_flags_dependency_rollback(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-bbbbbbbbbbbc"
    make_ledger(tmp_path, run_id)
    for phase in (
        "PREFLIGHT",
        "MERGED",
        "VERIFIED",
        "BUILT",
        "MACBOOK_STAGED",
        "STUDIO_ACTIVATED",
    ):
        SERVICE.transition(tmp_path, run_id, phase)
    SERVICE.transition(
        tmp_path,
        run_id,
        "STUDIO_ACTIVATED",
        dependency_sensitive_paths=["pyproject.toml", "uv.lock"],
    )
    (SERVICE.run_dir(tmp_path, run_id) / "bundle").mkdir(parents=True)

    SERVICE.execute_worker(tmp_path, tmp_path, run_id)

    ledger = SERVICE.read_json(SERVICE.ledger_path(tmp_path, run_id))
    assert ledger["status"] == "FAILED"
    assert "rollback requires dependency review" in ledger["error"]


def test_dependency_manifest_change_detection_is_exact_paths() -> None:
    assert SERVICE.dependency_manifests_changed(["package-lock.json"])
    assert SERVICE.dependency_manifests_changed(["apps/desktop/package.json"])
    assert SERVICE.dependency_manifests_changed(["web/package.json"])
    assert SERVICE.dependency_manifests_changed(["pyproject.toml"])
    assert SERVICE.dependency_manifests_changed(["uv.lock"])
    assert not SERVICE.dependency_manifests_changed(
        ["agent/agent_init.py", "docs/package.json", "vendor/x/package-lock.json"]
    )
    assert not SERVICE.dependency_manifests_changed(["web/package-lock.json"])


def test_materialize_node_dependencies_breaks_symlinks(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "20260723T120000Z-abcdefabcdef"
    make_ledger(tmp_path, run_id)
    worktree = tmp_path / "worktree"
    (worktree / "apps" / "desktop").mkdir(parents=True)
    real = tmp_path / "real-node-modules"
    real.mkdir()
    link = worktree / "node_modules"
    link.symlink_to(real, target_is_directory=True)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        SERVICE,
        "worker_command",
        lambda root, rid, args, cwd, name, timeout, **kw: commands.append(args),
    )

    SERVICE.materialize_node_dependencies(tmp_path, run_id, worktree)

    assert not link.exists() and not link.is_symlink()
    assert commands == [["npm", "ci"]]


def test_macbook_deferral_paths_exist_in_deploy() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'macbook_deferred=True' in source
    assert '"macbook_deferred_reason"' in source or "macbook_deferred_reason=" in source
    assert 'remote_state: Any = "deferred"' in source


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


def test_owned_command_failure_with_abort_marker_classifies_as_abort(
    tmp_path: Path,
) -> None:
    abort = tmp_path / "abort.request"
    abort.touch()

    with pytest.raises(SERVICE.AbortRequested):
        SERVICE.owned_command(
            [sys.executable, "-c", "raise SystemExit(3)"],
            tmp_path,
            tmp_path / "abort-classify.log",
            30,
            None,
            SERVICE.FD_LIMIT,
            abort_path=abort,
        )


def test_start_wait_and_kill_owned_child(tmp_path: Path) -> None:
    child, output = SERVICE.start_owned_child(
        [sys.executable, "-c", "print('background')"],
        tmp_path,
        tmp_path / "bg.log",
    )
    result = SERVICE.wait_owned_child(
        child, output, ["python"], 30, None, SERVICE.FD_LIMIT
    )
    assert result == 0
    assert output.closed
    assert "background" in (tmp_path / "bg.log").read_text(encoding="utf-8")

    sleeper, sleeper_output = SERVICE.start_owned_child(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        tmp_path / "bg-kill.log",
    )
    SERVICE.kill_owned_child(sleeper, sleeper_output)
    assert sleeper.poll() is not None
    assert sleeper_output.closed


def test_wait_owned_child_honors_abort_marker(tmp_path: Path) -> None:
    abort = tmp_path / "abort.request"
    abort.touch()
    child, output = SERVICE.start_owned_child(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        tmp_path / "bg-abort.log",
    )

    with pytest.raises(SERVICE.AbortRequested):
        SERVICE.wait_owned_child(
            child, output, ["python"], 60, None, SERVICE.FD_LIMIT, abort_path=abort
        )
    assert child.poll() is not None


def test_wait_worker_child_uses_ledger_budget(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-abcabcabcabc"
    make_ledger(tmp_path, run_id)
    child, output = SERVICE.start_owned_child(
        [sys.executable, "-c", "raise SystemExit(0)"],
        tmp_path,
        SERVICE.run_dir(tmp_path, run_id) / "evidence" / "bg-worker.log",
    )

    result = SERVICE.wait_worker_child(
        tmp_path, run_id, child, output, ["python"], 30
    )

    assert result == 0


def test_worker_failure_with_pending_abort_records_aborted(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-abcdefabcde0"
    make_ledger(tmp_path, run_id)
    SERVICE.transition(tmp_path, run_id, "PREFLIGHT")
    (SERVICE.run_dir(tmp_path, run_id) / "abort.request").touch()
    (SERVICE.run_dir(tmp_path, run_id) / "bundle").mkdir(parents=True)

    SERVICE.execute_worker(tmp_path, tmp_path, run_id)

    ledger = SERVICE.read_json(SERVICE.ledger_path(tmp_path, run_id))
    assert ledger["status"] == "ABORTED"
    assert ledger["error"].startswith("aborted during ")


def test_worker_failure_after_activation_ignores_abort_marker(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-abcdefabcde1"
    make_ledger(tmp_path, run_id)
    for phase in (
        "PREFLIGHT",
        "MERGED",
        "VERIFIED",
        "BUILT",
        "MACBOOK_STAGED",
        "STUDIO_ACTIVATED",
    ):
        SERVICE.transition(tmp_path, run_id, phase)
    (SERVICE.run_dir(tmp_path, run_id) / "abort.request").touch()
    (SERVICE.run_dir(tmp_path, run_id) / "bundle").mkdir(parents=True)

    SERVICE.execute_worker(tmp_path, tmp_path, run_id)

    ledger = SERVICE.read_json(SERVICE.ledger_path(tmp_path, run_id))
    assert ledger["status"] == "FAILED"


def test_full_validation_required_gates() -> None:
    assert SERVICE.full_validation_required(
        {"resolver_attempted": True}, [], []
    ) == (True, "merge resolver was attempted")

    many = [f"docs/f{index}.md" for index in range(6)]
    required, reason = SERVICE.full_validation_required(
        {"merge_conflicts": many}, [], []
    )
    assert required and "exceeds 5" in reason

    required, reason = SERVICE.full_validation_required(
        {"merge_conflicts": ["gateway/session.py"]}, [], []
    )
    assert required and "gateway/session.py" in reason

    required, reason = SERVICE.full_validation_required(
        {}, ["pyproject.toml"], ["pyproject.toml"]
    )
    assert required and "dependency manifests" in reason

    required, reason = SERVICE.full_validation_required(
        {}, ["scripts/run_tests.sh"], []
    )
    assert required and "scripts/run_tests.sh" in reason

    required, reason = SERVICE.full_validation_required(
        {}, ["tests/conftest.py"], []
    )
    assert required and "tests/conftest.py" in reason

    assert SERVICE.full_validation_required(
        {"merge_conflicts": ["docs/readme.md"]}, ["agent/x.py"], []
    ) == (False, "")


def test_full_validation_required_uses_parked_conflict_files() -> None:
    required, reason = SERVICE.full_validation_required(
        {"conflict_files": ["hermes_cli/main.py"]}, [], []
    )
    assert required and "hermes_cli/main.py" in reason


def test_consume_restart_outcome_marker(tmp_path: Path) -> None:
    run_id = "20260723T120000Z-abcdefabcde2"
    make_ledger(tmp_path, run_id)
    marker = SERVICE.run_dir(tmp_path, run_id) / "receipts" / "restart-outcome.json"
    evidence = SERVICE.run_dir(tmp_path, run_id) / "evidence" / "restart-outcome.json"

    SERVICE.consume_restart_outcome(tmp_path, run_id, marker)
    assert not evidence.is_file()

    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    SERVICE.atomic_json(marker, {"exit_code": 0, "message": "ok"})
    SERVICE.consume_restart_outcome(tmp_path, run_id, marker)
    assert SERVICE.read_json(evidence)["exit_code"] == 0

    SERVICE.atomic_json(
        marker,
        {"exit_code": 7, "message": "restart blew up", "log_path": "/tmp/r.log"},
    )
    with pytest.raises(RuntimeError, match=r"restart blew up.*(/tmp/r\.log)"):
        SERVICE.consume_restart_outcome(tmp_path, run_id, marker)
    assert SERVICE.read_json(evidence)["exit_code"] == 7


def test_tracked_children_reaped_on_resume(tmp_path: Path, monkeypatch) -> None:
    """A crashed worker's persisted background child is killed before resume
    relaunches it; a recycled pid (command mismatch) is left alone."""
    run_id = "20260723T120000Z-abcdefabcde7"
    make_ledger(tmp_path, run_id)

    class FakeChild:
        pid = 54321

    ps_answers = {"54321": "npm run dist:mac"}
    monkeypatch.setattr(
        SERVICE.subprocess,
        "run",
        lambda cmd, **_k: subprocess.CompletedProcess(
            cmd, 0, stdout=ps_answers.get(cmd[-1], "") + "\n", stderr=""
        ),
    )
    SERVICE.track_owned_child(tmp_path, run_id, "desktop-build", FakeChild())
    marker = SERVICE.children_dir(tmp_path, run_id) / "desktop-build.json"
    assert SERVICE.read_json(marker)["pid"] == 54321

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(SERVICE.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(SERVICE.time, "sleep", lambda _s: None)
    SERVICE.reap_tracked_children(tmp_path, run_id)
    assert (54321, SERVICE.signal.SIGTERM) in killed
    assert not marker.is_file()

    # Recycled pid: current command no longer matches the recorded one.
    SERVICE.track_owned_child(tmp_path, run_id, "desktop-build", FakeChild())
    ps_answers["54321"] = "some-unrelated-daemon"
    killed.clear()
    SERVICE.reap_tracked_children(tmp_path, run_id)
    assert killed == []
    assert not marker.is_file()

    SERVICE.track_owned_child(tmp_path, run_id, "live-npm-ci", FakeChild())
    SERVICE.untrack_owned_child(tmp_path, run_id, "live-npm-ci")
    assert not (SERVICE.children_dir(tmp_path, run_id) / "live-npm-ci.json").is_file()


def test_restart_enqueue_passes_completion_marker() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"--completion-marker"' in source
    assert "consume_restart_outcome(root, run_id, restart_outcome_marker)" in source


def test_live_dependency_refresh_overlaps_and_reaps_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "20260723T120000Z-abcdefabcde3"
    make_ledger(tmp_path, run_id)
    events: list[str] = []

    class FakeOutput:
        closed = False

        def close(self) -> None:
            self.closed = True

    class FakeChild:
        pid = 999999

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    def fake_start(command_args, cwd, log, env=None, child_fd_limit=0):
        events.append(f"start:{command_args[0]}")
        return FakeChild(), FakeOutput()

    def fake_wait(root, rid, child, output, command_args, timeout, **kwargs):
        events.append(f"wait:{command_args[0]}")
        if command_args[0] == "npm":
            raise subprocess.CalledProcessError(1, command_args)
        return 0

    monkeypatch.setattr(SERVICE, "start_owned_child", fake_start)
    monkeypatch.setattr(SERVICE, "wait_worker_child", fake_wait)
    monkeypatch.setattr(
        SERVICE,
        "kill_owned_child",
        lambda child, output: events.append("kill"),
    )

    with pytest.raises(subprocess.CalledProcessError):
        SERVICE.live_dependency_refresh(tmp_path, run_id, tmp_path)

    assert events == ["start:npm", "start:uv", "wait:npm", "kill", "kill"]


def test_live_dependency_refresh_success_waits_both(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "20260723T120000Z-abcdefabcde4"
    make_ledger(tmp_path, run_id)
    events: list[str] = []
    monkeypatch.setattr(
        SERVICE,
        "start_owned_child",
        lambda command_args, cwd, log, env=None, child_fd_limit=0: (
            events.append(f"start:{command_args[0]}"),
            events,
        ),
    )
    monkeypatch.setattr(
        SERVICE,
        "wait_worker_child",
        lambda root, rid, child, output, command_args, timeout, **kwargs: events.append(
            f"wait:{command_args[0]}"
        ),
    )
    monkeypatch.setattr(
        SERVICE,
        "track_owned_child",
        lambda root, rid, name, child: events.append(f"track:{name}"),
    )
    monkeypatch.setattr(
        SERVICE,
        "untrack_owned_child",
        lambda root, rid, name: events.append(f"untrack:{name}"),
    )

    SERVICE.live_dependency_refresh(tmp_path, run_id, tmp_path)

    assert events == [
        "start:npm",
        "track:live-npm-ci",
        "start:uv",
        "track:live-uv-sync",
        "wait:npm",
        "untrack:live-npm-ci",
        "wait:uv",
        "untrack:live-uv-sync",
    ]


def test_desktop_build_overlaps_validation_in_source() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "desktop_build_child, desktop_build_output = start_owned_child(" in source
    assert source.index("desktop_build_child, desktop_build_output") < source.index(
        '"curated-validation"'
    )
    assert "kill_owned_child(desktop_build_child, desktop_build_output)" in source


def test_deployed_carry_verify_is_probes_only_in_source() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    deployed = source.index('"deployed-carry-verify"')
    verify_argv = source.rindex('"--probes-only"', 0, deployed)

    assert verify_argv > source.index('"carry-verify"')


def test_validation_env_scoping_signals_in_source() -> None:
    service_source = SCRIPT.read_text(encoding="utf-8")
    script = SCRIPT.parent / "run_update_smart_client_validation.sh"
    shell_source = script.read_text(encoding="utf-8")

    assert '"UPDATE_CHANGED_DESKTOP"' in service_source
    assert '"UPDATE_VALIDATION_FULL"' in service_source
    assert "UPDATE_CHANGED_DESKTOP" in shell_source
    assert "UPDATE_VALIDATION_FULL" in shell_source
    bash_check = subprocess.run(
        ["/opt/homebrew/bin/bash", "-n", str(script)],
        text=True,
        capture_output=True,
    )
    assert bash_check.returncode == 0, bash_check.stderr
