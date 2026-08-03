#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import plistlib
import re
import resource
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable


DESCRIPTION = "Detached, resource-bounded Hermes slim update service."
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$")
CONFLICT_MARKER_RE = re.compile(r"^(?:<<<<<<< |=======|>>>>>>> )", re.MULTILINE)
TERMINAL = {"COMPLETED", "FAILED", "ABORTED"}
# Parked, not terminal: conflicts wait for resolution in the run's integration
# worktree, then the run resumes via the `resume` verb (or a service restart).
PARKED = "NEEDS_RESOLUTION"
PHASES = (
    "CREATED",
    "PREFLIGHT",
    "MERGED",
    "VERIFIED",
    "BUILT",
    "MACBOOK_STAGED",
    "STUDIO_ACTIVATED",
    "ARTIFACT_INSTALLED",
    "RESTARTED",
    "RUNTIME_VERIFIED",
    "COMPLETED",
)
MAX_PAYLOAD = 4096
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REQUEST_TTL = 60
FD_LIMIT = 256
VALIDATION_FD_LIMIT = 2048
DESKTOP_BUILD_FD_LIMIT = 2048
DESCENDANT_LIMIT = 64
WORKER_LIMIT = 2
REQUIRED_PORTS = (8642, 8787, 9119, 9120)
# Root-gateway listeners with no HTTP health endpoint: presence/replacement
# is required, but they are not HTTP-probed.
LISTENER_ONLY_PORTS = (8644, 8647)
# Best-effort surfaces are probed and recorded but never gate readiness or
# fail the run. Keep the required/best-effort split explicit here; moving a
# port between the two tuples is a deliberate policy change, not a tweak.
BEST_EFFORT_PORTS: tuple[int, ...] = ()
RESTART_WAIT_SECONDS = 7200
FAST_PATH_TARGET_SECONDS = 30 * 60
FAST_MAX_UPSTREAM_COMMITS = 100
FAST_MAX_CHANGED_PATHS = 1000
FAST_MAX_PREDICTED_CONFLICTS = 5
# Extras this machine's live venv is built with (matrix is deliberately
# excluded: python-olm does not build here). Keep in sync with the venv.
UV_SYNC_EXTRA_ARGS = (
    "--extra", "dev", "--extra", "messaging", "--extra", "anthropic",
    "--extra", "exa", "--extra", "firecrawl", "--extra", "fal",
    "--extra", "edge-tts", "--extra", "slack", "--extra", "wecom",
)
DEP_MANIFEST_NAMES = {
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "requirements.txt",
}
HTTP_PROBES = {
    8642: "/health",
    8787: "/health",
    9119: "/health",
    9120: "/",
}
EXPECTED_DESKTOP_TEAM = "SV9Z2RG2A6"
BUNDLE_FILES = (
    "scripts/hermes_update_service.py",
    "scripts/hermes_checkpoint.py",
    "scripts/carry.py",
    "scripts/thinning.py",
    "scripts/local_carry_manifest.yaml",
    "scripts/local_carry_exemptions.yaml",
    "scripts/run_tests.sh",
    "scripts/run_tests_parallel.py",
    "scripts/run_update_smart_client_validation.sh",
    "scripts/verify_deployed_turn.py",
)


class AbortRequested(RuntimeError):
    pass


class LeaseHeld(RuntimeError):
    pass


def utc_datetime() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def utc_now() -> str:
    return utc_datetime().isoformat()


def state_root(value: Path | None = None) -> Path:
    root = value or Path.home() / ".hermes" / "update-service"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temp = Path(handle.name)
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_dir(root: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("invalid run id")
    return root / "runs" / run_id


def ledger_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "ledger.json"


def phase_rank(name: str) -> float:
    # PARKED sits between PREFLIGHT (merge attempted) and MERGED (resolved).
    if name == PARKED:
        return PHASES.index("MERGED") - 0.5
    return float(PHASES.index(name))


def transition(root: Path, run_id: str, phase: str, **updates: Any) -> dict[str, Any]:
    path = ledger_path(root, run_id)
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = read_json(path)
        current = str(ledger["phase"])
        if phase not in PHASES and phase not in TERMINAL and phase != PARKED:
            raise ValueError(f"unknown phase: {phase}")
        if current in TERMINAL:
            raise RuntimeError(f"terminal run cannot transition: {current}")
        if phase not in TERMINAL and phase_rank(phase) < phase_rank(current):
            raise RuntimeError(f"phase regression: {current} -> {phase}")
        ledger.update(updates)
        ledger["phase"] = phase
        ledger["status"] = phase if (phase in TERMINAL or phase == PARKED) else "RUNNING"
        ledger.setdefault("phase_history", []).append({"phase": phase, "at": utc_now()})
        atomic_json(path, ledger)
        return ledger


def record(root: Path, run_id: str, **updates: Any) -> dict[str, Any]:
    """Update ledger fields without changing phase.

    Bookkeeping (checkpoint refs, resolver flags) must be re-recordable on
    resume; a parked run re-entering the merge block cannot transition back
    to PREFLIGHT (phase regression).
    """
    ledger = read_json(ledger_path(root, run_id))
    return transition(root, run_id, str(ledger["phase"]), **updates)


def phase_before(ledger: dict[str, Any], phase: str) -> bool:
    current = str(ledger["phase"])
    if current in TERMINAL:
        return False
    return phase_rank(current) < phase_rank(phase)


def advance(root: Path, run_id: str, phase: str, **updates: Any) -> dict[str, Any]:
    ledger = read_json(ledger_path(root, run_id))
    if phase_before(ledger, phase):
        return transition(root, run_id, phase, **updates)
    return ledger


def receipt_path(root: Path, run_id: str, name: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", name):
        raise ValueError("invalid receipt name")
    return run_dir(root, run_id) / "receipts" / f"{name}.json"


def prepare_receipt(
    root: Path,
    run_id: str,
    name: str,
    key: str,
    expected: dict[str, Any],
) -> dict[str, Any]:
    path = receipt_path(root, run_id, name)
    if path.is_file():
        existing = read_json(path)
        if existing.get("idempotency_key") != key:
            raise RuntimeError(f"receipt key mismatch: {name}")
        return existing
    payload = {
        "name": name,
        "state": "PREPARED",
        "idempotency_key": key,
        "expected": expected,
        "prepared_at": utc_now(),
    }
    atomic_json(path, payload)
    return payload


def complete_receipt(
    root: Path,
    run_id: str,
    name: str,
    observed: dict[str, Any],
) -> dict[str, Any]:
    path = receipt_path(root, run_id, name)
    payload = read_json(path)
    payload["state"] = "COMPLETED"
    payload["observed"] = observed
    payload["completed_at"] = utc_now()
    atomic_json(path, payload)
    return payload


def receipted(
    root: Path,
    run_id: str,
    name: str,
    key: str,
    expected: dict[str, Any],
    probe: Callable[[], tuple[bool, dict[str, Any]]],
    action: Callable[[], None],
    cancel: Callable[[], None] | None = None,
) -> dict[str, Any]:
    receipt = prepare_receipt(root, run_id, name, key, expected)
    if receipt.get("state") == "COMPLETED":
        return receipt
    matched, observed = probe()
    if not matched:
        if cancel:
            cancel()
        action()
        matched, observed = probe()
    if not matched:
        raise RuntimeError(f"side effect did not converge: {name}")
    return complete_receipt(root, run_id, name, observed)


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=check,
    )
    return result.stdout.strip()


def preview_merge_conflicts(repo: Path, base: str, upstream: str) -> tuple[list[str], str | None]:
    """Preview an exact ort merge without touching the index or worktree.

    ``git merge-tree --write-tree --name-only`` writes only temporary Git
    objects. On a conflicted merge its first section is the synthetic tree OID
    followed by one unresolved path per line; diagnostics begin after a blank
    line. Treat an unparseable result as unknown rather than guessing that the
    merge is clean.
    """
    try:
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--name-only", base, upstream],
            cwd=repo,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return [], "git merge-tree preview timed out after 300s"
    if result.returncode == 0:
        return [], None
    lines = result.stdout.splitlines()
    try:
        separator = lines.index("")
    except ValueError:
        separator = -1
    if result.returncode == 1 and separator > 1:
        return sorted(set(lines[1:separator])), None
    detail = (result.stderr or result.stdout).strip().splitlines()
    return [], (detail[-1][:300] if detail else f"git merge-tree exited {result.returncode}")


def preflight_assessment(
    repo: Path,
    base: str,
    upstream: str,
    *,
    fast_path_started_at: dt.datetime | None = None,
    fast_max_commits: int = FAST_MAX_UPSTREAM_COMMITS,
    fast_max_changed_paths: int = FAST_MAX_CHANGED_PATHS,
    fast_max_conflicts: int = FAST_MAX_PREDICTED_CONFLICTS,
) -> dict[str, Any]:
    """Classify a pinned update before creating its integration worktree."""
    commit_count = int(git(repo, "rev-list", "--count", f"{base}..{upstream}") or 0)
    changed_paths = git(repo, "diff", "--name-only", f"{base}...{upstream}").splitlines()
    conflicts, preview_error = preview_merge_conflicts(repo, base, upstream)
    reasons: list[str] = []
    if commit_count > fast_max_commits:
        reasons.append(
            f"upstream commit count {commit_count} exceeds fast limit {fast_max_commits}"
        )
    if len(changed_paths) > fast_max_changed_paths:
        reasons.append(
            f"upstream changed-path count {len(changed_paths)} exceeds fast limit "
            f"{fast_max_changed_paths}"
        )
    if len(conflicts) > fast_max_conflicts:
        reasons.append(
            f"predicted conflict count {len(conflicts)} exceeds fast limit "
            f"{fast_max_conflicts}"
        )
    if preview_error:
        reasons.append(f"merge conflict preview unavailable: {preview_error}")
    assessed_at = utc_datetime()
    target_started_at = fast_path_started_at or assessed_at
    update_class = "LARGE" if reasons else "FAST"
    return {
        "update_class": update_class,
        "classification_reasons": reasons,
        "assessed_at": assessed_at.isoformat(),
        "fast_path_started_at": target_started_at.isoformat(),
        "fast_path_target_seconds": FAST_PATH_TARGET_SECONDS,
        "fast_path_deadline": (
            (
                target_started_at + dt.timedelta(seconds=FAST_PATH_TARGET_SECONDS)
            ).isoformat()
            if update_class == "FAST"
            else None
        ),
        "upstream_commit_count": commit_count,
        "upstream_changed_path_count": len(changed_paths),
        "predicted_conflict_count": len(conflicts),
        "predicted_conflicts": conflicts,
        "merge_preview_error": preview_error,
    }


def effective_fast_path_classification(ledger: dict[str, Any]) -> dict[str, Any]:
    """Derive an overdue LARGE classification without mutating the ledger."""
    deadline_raw = ledger.get("fast_path_deadline")
    if (
        ledger.get("status") in TERMINAL
        or ledger.get("update_class") != "FAST"
        or not isinstance(deadline_raw, str)
    ):
        return ledger
    try:
        deadline = dt.datetime.fromisoformat(deadline_raw)
    except ValueError:
        return ledger
    now = utc_datetime()
    if now <= deadline:
        return ledger
    effective = dict(ledger)
    reasons = list(ledger.get("classification_reasons") or [])
    reasons.append("30-minute target elapsed before fast-path completion")
    effective.update(
        update_class="LARGE",
        classification_reasons=reasons,
        fast_path_missed_at=now.isoformat(),
    )
    return effective


def refresh_fast_path_classification(root: Path, run_id: str) -> dict[str, Any]:
    """Persist a missed FAST target from the single update worker."""
    ledger = read_json(ledger_path(root, run_id))
    effective = effective_fast_path_classification(ledger)
    if effective is ledger:
        return ledger
    try:
        return record(
            root,
            run_id,
            update_class=effective["update_class"],
            classification_reasons=effective["classification_reasons"],
            fast_path_missed_at=effective["fast_path_missed_at"],
        )
    except RuntimeError:
        # An abort can race the worker between the read above and record()'s
        # locked update. Terminal ledgers are immutable; return the winner
        # rather than turning a successful worker command into an error.
        latest = read_json(ledger_path(root, run_id))
        if latest.get("status") in TERMINAL:
            return latest
        raise


def fd_count(pid: int) -> int:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-n", "-p", str(pid)],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        return 0
    return max(0, len(result.stdout.splitlines()) - 1)


def children(pid: int) -> set[int]:
    found: set[int] = set()
    pending = [pid]
    while pending:
        parent = pending.pop()
        result = subprocess.run(
            ["pgrep", "-P", str(parent)],
            text=True,
            capture_output=True,
        )
        for raw in result.stdout.split():
            child = int(raw)
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def parent_pid(pid: int) -> int | None:
    result = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(pid)],
        text=True,
        capture_output=True,
    )
    value = result.stdout.strip()
    return int(value) if value.isdigit() and int(value) > 1 else None


def process_snapshot(pid: int, origin_pid: int | None = None) -> dict[str, int]:
    payload = {
        "pid": pid,
        "fds": fd_count(pid),
        "descendants": len(children(pid)),
    }
    if origin_pid:
        payload["origin_pid"] = origin_pid
        payload["origin_fds"] = fd_count(origin_pid)
    return payload


def listener_pids() -> dict[int, int]:
    result: dict[int, int] = {}
    for port in (*REQUIRED_PORTS, *LISTENER_ONLY_PORTS, *BEST_EFFORT_PORTS):
        probe = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            text=True,
            capture_output=True,
        )
        values = [int(value) for value in probe.stdout.split() if value.isdigit()]
        if values:
            result[port] = values[0]
    return result


def http_ready(port: int) -> bool:
    path = HTTP_PROBES[port]
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def surface_inventory(
    old_pids: dict[int, int] | None = None,
) -> tuple[bool, dict[str, Any]]:
    current = listener_pids()
    ready = {
        port: http_ready(port) for port in (*REQUIRED_PORTS, *BEST_EFFORT_PORTS)
    }
    replaced = {
        port: (
            port in current
            and (not old_pids or not old_pids.get(port) or current[port] != old_pids[port])
        )
        for port in (*REQUIRED_PORTS, *LISTENER_ONLY_PORTS)
    }
    healthy = all(ready[port] for port in REQUIRED_PORTS) and all(replaced.values())
    return healthy, {
        "listener_pids": current,
        "http_ready": ready,
        "replaced": replaced,
    }


def wait_for_surfaces(
    root: Path,
    run_id: str,
    probe: Callable[[], tuple[bool, dict[str, Any]]],
    wait_seconds: float,
    poll_seconds: float = 5,
    audit_seconds: float = 60,
    sleeper: Callable[[float], None] = time.sleep,
    busy_probe: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """Poll readiness with an observable drain audit.

    While waiting, append a per-interval snapshot (listener PIDs, readiness,
    replacement state) to evidence/drain-audit.jsonl so a long drain is
    diagnosable instead of a silent hang. On timeout, name the blocked ports.

    ``busy_probe``, when given, is called at the same audit cadence and its
    return value is embedded under the "busy" key — the structured
    per-target explanation (label, active_agents, gateway_state,
    restart_requested, freshness age) of what is actually holding the drain
    open, not just which ports aren't ready yet. A probe failure is recorded
    as an error string rather than raised: a diagnostic collection failure
    must never fail or block the drain wait itself.
    """
    audit_path = run_dir(root, run_id) / "evidence" / "drain-audit.jsonl"
    audit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    last_audit = float("-inf")
    details: dict[str, Any] = {}
    while time.monotonic() < deadline:
        matched, details = probe()
        if matched:
            return
        if time.monotonic() - last_audit >= audit_seconds:
            last_audit = time.monotonic()
            entry: dict[str, Any] = {"at": utc_now(), **details}
            if busy_probe is not None:
                try:
                    entry["busy"] = busy_probe()
                except Exception as exc:
                    entry["busy"] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        sleeper(poll_seconds)
    blocked = sorted(
        {
            str(port)
            for port, ok in details.get("http_ready", {}).items()
            if not ok and port in REQUIRED_PORTS
        }
        | {str(port) for port, ok in details.get("replaced", {}).items() if not ok}
    )
    raise RuntimeError(
        "Hermes readiness did not return; blocked ports: "
        + (", ".join(blocked) or "unknown")
        + f"; drain audit: {audit_path}"
    )


def enforce_budget(pid: int, origin_pid: int | None, origin_fd_limit: int) -> dict[str, int]:
    snapshot = process_snapshot(pid, origin_pid)
    if snapshot["fds"] > FD_LIMIT:
        raise RuntimeError(f"service FD budget exceeded: {snapshot['fds']}>{FD_LIMIT}")
    if snapshot["descendants"] > DESCENDANT_LIMIT:
        raise RuntimeError(
            f"descendant budget exceeded: {snapshot['descendants']}>{DESCENDANT_LIMIT}"
        )
    if origin_pid and snapshot.get("origin_fds", 0) > origin_fd_limit:
        raise RuntimeError(
            f"origin session FD budget exceeded: {snapshot['origin_fds']}>{origin_fd_limit}"
        )
    return snapshot


def start_owned_child(
    command_args: list[str],
    cwd: Path,
    log: Path,
    env: dict[str, str] | None = None,
    child_fd_limit: int = FD_LIMIT,
) -> tuple[subprocess.Popen[bytes], Any]:
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = log.open("ab", buffering=0)
    try:
        child = subprocess.Popen(
            command_args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            preexec_fn=lambda: resource.setrlimit(
                resource.RLIMIT_NOFILE,
                (child_fd_limit, child_fd_limit),
            ),
        )
    except Exception:
        output.close()
        raise
    return child, output


def kill_owned_child(child: subprocess.Popen[bytes], output: Any) -> None:
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        time.sleep(1)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
    child.wait()
    if not output.closed:
        output.close()


def wait_owned_child(
    child: subprocess.Popen[bytes],
    output: Any,
    command_args: list[str],
    timeout: int,
    origin_pid: int | None,
    origin_fd_limit: int,
    abort_path: Path | None = None,
    allow_failure: bool = False,
) -> int:
    try:
        deadline = time.monotonic() + timeout
        while child.poll() is None:
            if abort_path and abort_path.exists():
                kill_owned_child(child, output)
                raise AbortRequested("abort requested")
            if time.monotonic() >= deadline:
                os.killpg(child.pid, signal.SIGTERM)
                time.sleep(2)
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                raise TimeoutError(f"command timed out after {timeout}s")
            try:
                enforce_budget(os.getpid(), origin_pid, origin_fd_limit)
            except Exception:
                os.killpg(child.pid, signal.SIGTERM)
                time.sleep(1)
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                raise
            time.sleep(0.5)
        if child.returncode and not allow_failure:
            # A SIGTERM-killed child exits non-zero; an armed abort marker is
            # the real cause, not the command failure.
            if abort_path and abort_path.exists():
                raise AbortRequested("abort requested")
            raise subprocess.CalledProcessError(child.returncode, command_args)
        return int(child.returncode or 0)
    finally:
        if not output.closed:
            output.close()


def owned_command(
    command_args: list[str],
    cwd: Path,
    log: Path,
    timeout: int,
    origin_pid: int | None,
    origin_fd_limit: int,
    env: dict[str, str] | None = None,
    abort_path: Path | None = None,
    allow_failure: bool = False,
    child_fd_limit: int = FD_LIMIT,
) -> int:
    child, output = start_owned_child(command_args, cwd, log, env, child_fd_limit)
    return wait_owned_child(
        child,
        output,
        command_args,
        timeout,
        origin_pid,
        origin_fd_limit,
        abort_path,
        allow_failure,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_bundle(
    repo: Path,
    destination: Path,
    service_source: Path,
) -> dict[str, str]:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    hashes: dict[str, str] = {}
    for relative in BUNDLE_FILES:
        source = (
            service_source
            if relative == "scripts/hermes_update_service.py"
            else repo / relative
        )
        target = destination / Path(relative).name
        shutil.copy2(source, target)
        os.chmod(target, 0o500 if source.suffix in {".py", ".sh"} else 0o400)
        hashes[target.name] = sha256(target)
    atomic_json(destination / "bundle-manifest.json", {"version": 1, "files": hashes})
    os.chmod(destination / "bundle-manifest.json", 0o400)
    return hashes


def verify_bundle(bundle: Path) -> None:
    manifest = read_json(bundle / "bundle-manifest.json")
    for name, expected in manifest["files"].items():
        if sha256(bundle / name) != expected:
            raise RuntimeError(f"pinned updater bundle hash mismatch: {name}")


def new_run(
    repo: Path,
    root: Path,
    mode: str,
    origin_pid: int | None,
    curated_override: str | None = None,
    pin_upstream: str | None = None,
) -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{uuid.uuid4().hex[:12]}"
    directory = run_dir(root, run_id)
    directory.mkdir(mode=0o700, parents=True)
    os.chmod(directory, 0o700)
    bundle = directory / "bundle"
    hashes = create_bundle(repo, bundle, Path(__file__).resolve())
    base = git(repo, "rev-parse", "HEAD")
    monitored_origin = parent_pid(origin_pid) if origin_pid else None
    monitored_origin = monitored_origin or origin_pid
    ledger = {
        "version": 1,
        "run_id": run_id,
        "mode": mode,
        "status": "RUNNING",
        "phase": "CREATED",
        "phase_history": [{"phase": "CREATED", "at": utc_now()}],
        "base_commit": base,
        "branch": git(repo, "branch", "--show-current"),
        "origin_pid": monitored_origin,
        "origin_fd_baseline": fd_count(monitored_origin) if monitored_origin else None,
        "service_pid": os.getpid(),
        "service_started": int(ps_start_time(os.getpid())),
        "bundle_hashes": hashes,
        "created_at": utc_now(),
    }
    if curated_override:
        ledger["curated_override"] = curated_override
    if pin_upstream:
        # Pre-recording upstream_sha makes the worker skip its fetch and merge
        # exactly this commit — the operator pin for busy upstream days. The
        # sha must already be reachable locally (a prior run fetched it).
        ledger["upstream_sha"] = pin_upstream
        ledger["upstream_pinned"] = True
    atomic_json(ledger_path(root, run_id), ledger)
    return run_id


def ps_start_time(pid: int) -> float:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        text=True,
        capture_output=True,
    )
    if result.returncode or not result.stdout.strip():
        return 0
    parsed = time.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y")
    return time.mktime(parsed)


def active_run(root: Path) -> str | None:
    runs = root / "runs"
    if not runs.is_dir():
        return None
    for path in sorted(runs.glob("*/ledger.json"), reverse=True):
        try:
            ledger = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if ledger.get("status") not in TERMINAL:
            return str(ledger["run_id"])
    return None


def peer_identity(connection: socket.socket) -> tuple[int, int | None]:
    if hasattr(connection, "getpeereid"):
        uid, _ = connection.getpeereid()
    elif sys.platform == "darwin":
        credentials = connection.getsockopt(0, socket.LOCAL_PEERCRED, 256)
        uid = struct.unpack_from("I", credentials, 4)[0]
    else:
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _, uid, _ = struct.unpack("3i", credentials)
    peer_pid: int | None = None
    if sys.platform == "darwin":
        try:
            peer_pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
        except OSError:
            pass
    return uid, peer_pid


def nonce_valid(root: Path, nonce: str, timestamp: int) -> bool:
    if not re.fullmatch(r"[a-f0-9]{32}", nonce):
        return False
    now = int(time.time())
    if abs(now - timestamp) > REQUEST_TTL:
        return False
    path = root / "nonces.json"
    entries: dict[str, int] = {}
    if path.is_file():
        try:
            entries = {
                key: int(value)
                for key, value in read_json(path).items()
                if int(value) >= now - REQUEST_TTL
            }
        except (OSError, json.JSONDecodeError, ValueError):
            entries = {}
    if nonce in entries:
        return False
    entries[nonce] = timestamp
    atomic_json(path, entries)
    return True


def redacted_status(root: Path, run_id: str) -> dict[str, Any]:
    ledger = effective_fast_path_classification(read_json(ledger_path(root, run_id)))
    allowed = {
        "run_id",
        "mode",
        "status",
        "phase",
        "base_commit",
        "upstream_sha",
        "result_commit",
        "created_at",
        "completed_at",
        "error",
        "conflict_files",
        "worktree",
        "dependency_sensitive_paths",
        "macbook_deferred",
        "macbook_deferred_reason",
        "retired",
        "retired_at",
        "update_class",
        "classification_reasons",
        "assessed_at",
        "fast_path_started_at",
        "fast_path_target_seconds",
        "fast_path_deadline",
        "fast_path_missed_at",
        "upstream_commit_count",
        "upstream_changed_path_count",
        "predicted_conflict_count",
        "merge_preview_error",
    }
    return {key: ledger[key] for key in allowed if key in ledger}


def spawn_worker(repo: Path, root: Path, run_id: str) -> None:
    pinned = run_dir(root, run_id) / "bundle" / "hermes_update_service.py"
    verify_bundle(pinned.parent)
    log = run_dir(root, run_id) / "worker.log"
    with log.open("ab", buffering=0) as output:
        subprocess.Popen(
            [
                sys.executable,
                str(pinned),
                "--repo",
                str(repo),
                "--state-root",
                str(root),
                "run-worker",
                "--run-id",
                run_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )


def handle_request(repo: Path, root: Path, payload: dict[str, Any], peer_pid: int | None) -> dict[str, Any]:
    if set(payload) - {"verb", "mode", "run_id", "nonce", "timestamp", "curated_override", "pin_upstream"}:
        raise ValueError("unknown request field")
    verb = payload.get("verb")
    if verb not in {"start", "status", "abort", "resume"}:
        raise ValueError("invalid verb")
    nonce = payload.get("nonce")
    timestamp = payload.get("timestamp")
    if not isinstance(nonce, str) or not isinstance(timestamp, int):
        raise ValueError("nonce and timestamp are required")
    if not nonce_valid(root, nonce, timestamp):
        raise PermissionError("stale or replayed request")
    if verb == "start":
        if set(payload) - {"verb", "mode", "nonce", "timestamp", "curated_override", "pin_upstream"}:
            raise ValueError("start payload is invalid")
        mode = payload.get("mode")
        if mode not in {"rehearse", "update"}:
            raise ValueError("invalid mode")
        curated_override = payload.get("curated_override")
        if curated_override is not None:
            if (
                not isinstance(curated_override, str)
                or not curated_override.strip()
                or len(curated_override) > 300
            ):
                raise ValueError("curated_override must be a short non-empty reason")
            curated_override = curated_override.strip()
        pin_upstream = payload.get("pin_upstream")
        if pin_upstream is not None:
            if not isinstance(pin_upstream, str) or not re.fullmatch(
                r"[0-9a-f]{40}", pin_upstream.strip()
            ):
                raise ValueError("pin_upstream must be a full 40-hex commit sha")
            pin_upstream = pin_upstream.strip()
        prior = active_run(root)
        if prior:
            raise RuntimeError(f"run already active: {prior}")
        run_id = new_run(
            repo,
            root,
            mode,
            peer_pid,
            curated_override=curated_override,
            pin_upstream=pin_upstream,
        )
        spawn_worker(repo, root, run_id)
        return {"ok": True, "run_id": run_id}
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("valid run_id required")
    if verb == "status":
        return {"ok": True, "run": redacted_status(root, run_id)}
    if verb == "resume":
        ledger = read_json(ledger_path(root, run_id))
        status = str(ledger.get("status"))
        if status in TERMINAL:
            raise RuntimeError(f"run is terminal: {status}")
        if lease_is_live(root):
            raise RuntimeError("worker already live for the update lease")
        spawn_worker(repo, root, run_id)
        return {"ok": True, "run_id": run_id, "resumed": True}
    abort = run_dir(root, run_id) / "abort.request"
    abort.touch(mode=0o600, exist_ok=True)
    ledger = read_json(ledger_path(root, run_id))
    status = str(ledger.get("status"))
    if status not in TERMINAL and not lease_is_live(root):
        try:
            pre_activation = phase_rank(str(ledger.get("phase"))) < phase_rank(
                "STUDIO_ACTIVATED"
            )
        except ValueError:
            pre_activation = False
        if pre_activation:
            # No worker will ever consume the marker (e.g. a parked run whose
            # worker already returned): finalize the abort here so the run
            # stops blocking future starts and updater reinstalls. Runs at or
            # past activation must be resumed and rolled forward, not aborted.
            transition(
                root,
                run_id,
                "ABORTED",
                error="aborted while no worker owned the run",
            )
            return {"ok": True, "run_id": run_id, "aborted": True}
    return {"ok": True, "run_id": run_id, "abort_requested": True}


def serve(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    root = state_root(args.state_root)
    socket_path = root / "update.sock"
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(8)
    interrupted = active_run(root)
    if interrupted:
        spawn_worker(repo, root, interrupted)
    while True:
        connection, _ = server.accept()
        with connection:
            try:
                connection.settimeout(2)
                uid, peer_pid = peer_identity(connection)
                if uid != os.getuid():
                    raise PermissionError("unauthorized caller")
                raw = connection.recv(MAX_PAYLOAD + 1)
                if len(raw) > MAX_PAYLOAD:
                    raise ValueError("payload too large")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                response = handle_request(repo, root, payload, peer_pid)
            except Exception as exc:
                response = {"ok": False, "error": type(exc).__name__, "message": str(exc)[:240]}
            connection.sendall((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))


def receive_response(client: socket.socket) -> str:
    """Read one newline-delimited response without truncating large ledgers."""
    raw = bytearray()
    while b"\n" not in raw:
        remaining = MAX_RESPONSE_BYTES - len(raw) + 1
        chunk = client.recv(min(64 * 1024, remaining))
        if not chunk:
            raise ConnectionError("update service closed before completing response")
        raw.extend(chunk)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("update service response too large")
    response, _, _ = raw.partition(b"\n")
    return response.decode("utf-8")


def request(args: argparse.Namespace) -> int:
    root = state_root(args.state_root)
    payload: dict[str, Any] = {
        "verb": args.verb,
        "nonce": uuid.uuid4().hex,
        "timestamp": int(time.time()),
    }
    if args.verb == "start":
        payload["mode"] = args.mode
        if getattr(args, "curated_override", None):
            payload["curated_override"] = args.curated_override
        if getattr(args, "pin_upstream", None):
            payload["pin_upstream"] = args.pin_upstream
    else:
        payload["run_id"] = args.run_id
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(str(root / "update.sock"))
    client.sendall(json.dumps(payload).encode("utf-8"))
    response = receive_response(client)
    print(response.strip())
    return 0 if json.loads(response).get("ok") else 1


def current_lease(root: Path) -> dict[str, Any] | None:
    path = root / "lease.json"
    if not path.is_file():
        return None
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        # A released lease is truncated to zero bytes but left in place.
        return None
    return payload if payload else None


def lease_is_live(root: Path) -> bool:
    lease = current_lease(root)
    if not lease:
        return False
    pid = int(lease.get("pid") or 0)
    expected_start = float(lease.get("start_time") or 0)
    return pid > 0 and pid != os.getpid() and ps_start_time(pid) == expected_start


def acquire_lease(root: Path, run_id: str):
    path = root / "lease.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise LeaseHeld("update lease is held by another worker")
    handle.seek(0)
    raw = handle.read()
    if raw:
        try:
            existing = json.loads(raw)
        except json.JSONDecodeError:
            existing = {}
        pid = int(existing.get("pid", 0))
        expected_start = float(existing.get("start_time", 0))
        if pid > 0 and pid != os.getpid() and ps_start_time(pid) == expected_start:
            handle.close()
            raise LeaseHeld(f"lease held by live process {pid}")
    payload = {
        "run_id": run_id,
        "pid": os.getpid(),
        "start_time": ps_start_time(os.getpid()),
        "acquired_at": utc_now(),
    }
    handle.seek(0)
    handle.truncate()
    json.dump(payload, handle, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
    os.chmod(path, 0o600)
    return handle


def release_lease(handle) -> None:
    if handle is None:
        return
    handle.seek(0)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())
    fcntl.flock(handle, fcntl.LOCK_UN)
    handle.close()


def worker_command(
    root: Path,
    run_id: str,
    command_args: list[str],
    cwd: Path,
    name: str,
    timeout: int,
    env: dict[str, str] | None = None,
    allow_failure: bool = False,
    honor_abort: bool = True,
    child_fd_limit: int = FD_LIMIT,
) -> int:
    refresh_fast_path_classification(root, run_id)
    ledger = read_json(ledger_path(root, run_id))
    origin_pid = ledger.get("origin_pid")
    baseline = int(ledger.get("origin_fd_baseline") or 0)
    origin_limit = max(FD_LIMIT, baseline + 32)
    result = owned_command(
        command_args,
        cwd,
        run_dir(root, run_id) / "evidence" / f"{name}.log",
        timeout,
        int(origin_pid) if origin_pid else None,
        origin_limit,
        env,
        run_dir(root, run_id) / "abort.request" if honor_abort else None,
        allow_failure,
        child_fd_limit,
    )
    refresh_fast_path_classification(root, run_id)
    return result


def wait_worker_child(
    root: Path,
    run_id: str,
    child: subprocess.Popen[bytes],
    output: Any,
    command_args: list[str],
    timeout: int,
    honor_abort: bool = True,
    allow_failure: bool = False,
) -> int:
    ledger = read_json(ledger_path(root, run_id))
    origin_pid = ledger.get("origin_pid")
    baseline = int(ledger.get("origin_fd_baseline") or 0)
    origin_limit = max(FD_LIMIT, baseline + 32)
    return wait_owned_child(
        child,
        output,
        command_args,
        timeout,
        int(origin_pid) if origin_pid else None,
        origin_limit,
        run_dir(root, run_id) / "abort.request" if honor_abort else None,
        allow_failure,
    )


def children_dir(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "children"


def track_owned_child(
    root: Path, run_id: str, name: str, child: subprocess.Popen[bytes]
) -> None:
    """Persist a background child's identity so a crashed worker's orphans can
    be reconciled on resume instead of racing a duplicate against the same
    output tree (start_new_session detaches them from the worker's fate)."""
    try:
        command = subprocess.run(
            ["ps", "-o", "command=", "-p", str(child.pid)],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError:
        command = ""
    atomic_json(
        children_dir(root, run_id) / f"{name}.json",
        {"pid": child.pid, "command": command, "started_at": utc_now()},
    )


def untrack_owned_child(root: Path, run_id: str, name: str) -> None:
    (children_dir(root, run_id) / f"{name}.json").unlink(missing_ok=True)


def reap_tracked_children(root: Path, run_id: str) -> None:
    """Kill any background children a previous worker left behind.

    Only kills a recorded pid when its current command line still matches the
    recorded one, so a recycled pid is never signalled."""
    directory = children_dir(root, run_id)
    if not directory.is_dir():
        return
    for marker in sorted(directory.glob("*.json")):
        try:
            entry = read_json(marker)
        except (OSError, json.JSONDecodeError):
            marker.unlink(missing_ok=True)
            continue
        pid = entry.get("pid")
        recorded = entry.get("command") or ""
        if isinstance(pid, int) and pid > 1 and recorded:
            current = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if current == recorded:
                try:
                    os.killpg(pid, signal.SIGTERM)
                    time.sleep(2)
                    os.killpg(pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        marker.unlink(missing_ok=True)


def abort_requested(root: Path, run_id: str) -> bool:
    return (run_dir(root, run_id) / "abort.request").exists()


def ensure_not_aborted(root: Path, run_id: str) -> None:
    if abort_requested(root, run_id):
        raise AbortRequested("abort requested")


def abort_before_activation(root: Path, run_id: str) -> None:
    ledger = read_json(ledger_path(root, run_id))
    if phase_before(ledger, "STUDIO_ACTIVATED"):
        ensure_not_aborted(root, run_id)


def conflict_is_resolved(path: Path) -> bool:
    """Only marker-free regular text files auto-stage.

    Binary files carry no conflict markers, and delete/modify conflicts leave
    no file at all — auto-staging either silently picks a side. Both park for
    explicit resolution instead.
    """
    if not path.is_file() or path.is_symlink():
        return False
    data = path.read_bytes()
    if b"\x00" in data:
        return False
    return not CONFLICT_MARKER_RE.search(data.decode("utf-8", errors="replace"))


def worktree_for(root: Path, run_id: str) -> Path:
    return root / "worktrees" / run_id


def remove_rehearsal_worktree(repo: Path, worktree: Path) -> None:
    """Make a successful rehearsal terminal without leaving a checkout behind."""
    if not worktree.exists():
        return
    git(repo, "worktree", "remove", "--force", str(worktree))
    git(repo, "worktree", "prune")


NODE_DEPENDENCY_TREES = (
    Path("node_modules"),
    Path("web/node_modules"),
    Path("apps/desktop/node_modules"),
)


# Language-scoped: a JS manifest bump must escalate the JS validation lane,
# never the ~45k-test python suite (2026-07-24: five package.json changes
# triggered an ~8h python-full run that validated nothing those manifests
# touch). Python manifests escalate python-full as before.
PYTHON_DEPENDENCY_MANIFEST_PATHS = {
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
}
JS_DEPENDENCY_MANIFEST_SUFFIXES = ("package.json", "package-lock.json")
# Resolver-authored resolutions confined to these suffixes ride the JS lane
# (tsc/build) + carry-verify instead of escalating the ~45k-test python suite.
RESOLVER_JS_SAFE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".css", ".scss", ".md")
DEPENDENCY_MANIFEST_PATHS = PYTHON_DEPENDENCY_MANIFEST_PATHS | {
    "package.json",
    "package-lock.json",
    "web/package.json",
    "apps/desktop/package.json",
}


def js_dependency_manifests_changed(changed: list[str]) -> list[str]:
    return sorted(
        path for path in changed if path.endswith(JS_DEPENDENCY_MANIFEST_SUFFIXES)
    )


def python_dependency_manifests_changed(changed: list[str]) -> list[str]:
    return sorted(set(changed) & PYTHON_DEPENDENCY_MANIFEST_PATHS)
FULL_VALIDATION_CONFLICT_PREFIXES = ("agent/", "gateway/", "hermes_cli/", "tools/")
FULL_VALIDATION_HARNESS_PATHS = {
    "tests/conftest.py",
    "scripts/run_tests.sh",
    "scripts/local_carry_manifest.yaml",
}


def dependency_manifests_changed(changed: list[str]) -> bool:
    return any(path in DEPENDENCY_MANIFEST_PATHS for path in changed)


def effective_validation(
    ledger: dict[str, Any], required: bool, reason: str
) -> tuple[bool, str]:
    """Keep service updates on bounded validation unless explicitly overridden.

    Automatic full-suite escalation has repeatedly turned routine updates into
    multi-hour runs and then failed from host contention rather than product
    regressions.  ``full_validation_required`` remains a risk classifier, but
    the transactional service records its recommendation and stays on the
    curated/carry/language-specific lanes.  A full suite is an attended,
    separately-invoked diagnostic, never a hidden phase of an update.

    ``curated_override`` remains useful as the operator's audited explanation;
    absent one, the service records that its bounded-validation policy made the
    decision.
    """
    if not required:
        return False, reason
    override = str(ledger.get("curated_override") or "").strip()
    if override:
        return False, f"CURATED-OVERRIDE({override}); suppressed: {reason}"
    return False, f"CURATED-DEFAULT; full-suite recommendation suppressed: {reason}"


def full_validation_required(
    ledger: dict[str, Any],
    changed: list[str],
    dependency_sensitive: list[str],
) -> tuple[bool, str]:
    """Escalate the curated validation to the full suite when the merge was
    risky enough that scoping is no longer trustworthy."""
    conflicts = [
        str(path)
        for path in (ledger.get("merge_conflicts") or ledger.get("conflict_files") or [])
    ]
    if ledger.get("resolver_attempted"):
        # Language-scope the resolver escalation like the manifest gate: a
        # resolver-authored resolution in JS/TS/i18n/docs cannot regress
        # python, and those surfaces are exercised by the JS lane (npm ci,
        # desktop/web builds incl. tsc) plus carry-verify. Escalate to the
        # python suite only when a resolved conflict touches anything else —
        # or when the conflict list is empty (unknown scope, trust nothing).
        unsafe = [
            path
            for path in conflicts
            if not path.endswith(RESOLVER_JS_SAFE_SUFFIXES)
        ]
        if unsafe or not conflicts:
            return True, "merge resolver was attempted"
    if len(conflicts) > 5:
        return True, f"resolved conflict count {len(conflicts)} exceeds 5"
    hot = [
        path for path in conflicts if path.startswith(FULL_VALIDATION_CONFLICT_PREFIXES)
    ]
    if hot:
        return True, "conflicts touch core paths: " + ", ".join(hot[:5])
    # JS manifests do NOT justify the python suite: the JS lane (npm ci +
    # desktop/web batches) already runs whenever the diff touches those
    # surfaces, and package.json cannot regress python behavior. The
    # dependency_sensitive list is collected language-blind (any manifest
    # basename or *.lock), so it must be re-scoped here — run-3 2026-07-24
    # escalated to the ~8h python suite on five package.json changes because
    # the unfiltered list short-circuited ahead of the scoped check.
    js_suffixes = JS_DEPENDENCY_MANIFEST_SUFFIXES + ("yarn.lock", "pnpm-lock.yaml")
    python_manifests = sorted(
        set(python_dependency_manifests_changed(changed))
        | {
            path
            for path in dependency_sensitive
            if not path.endswith(js_suffixes)
        }
    )
    if python_manifests:
        return True, "python dependency manifests changed: " + ", ".join(
            python_manifests[:5]
        )
    harness = sorted(FULL_VALIDATION_HARNESS_PATHS.intersection(changed))
    if harness:
        return True, "validation harness changed: " + ", ".join(harness)
    return False, ""


def materialize_node_dependencies(
    root: Path, run_id: str, worktree: Path
) -> None:
    """Replace stale node_modules symlinks with a real install.

    The worktree symlinks the live checkout's node_modules for speed, but a
    merge that changes package manifests can require binaries the old install
    lacks (2026-07-23: upstream added cross-env; every UI carry test died on
    `command not found`). When manifests changed, break the links and run one
    `npm ci` against the merged lockfile.
    """
    for relative in NODE_DEPENDENCY_TREES:
        target = worktree / relative
        if target.is_symlink():
            target.unlink()
    worker_command(
        root,
        run_id,
        ["npm", "ci"],
        worktree,
        "npm-ci",
        3600,
        child_fd_limit=DESKTOP_BUILD_FD_LIMIT,
    )


def link_checkout_dependencies(repo: Path, worktree: Path) -> None:
    for relative in (
        Path("node_modules"),
        Path("web/node_modules"),
        Path("apps/desktop/node_modules"),
    ):
        source = repo / relative
        target = worktree / relative
        if not source.is_dir() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source, target_is_directory=True)


def desktop_identity(app: Path) -> dict[str, str]:
    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        text=True,
        capture_output=True,
    )
    if verify.returncode:
        raise RuntimeError(f"Desktop artifact signature invalid: {verify.stderr[-500:]}")
    details = subprocess.run(
        ["codesign", "-dv", "--verbose=4", str(app)],
        text=True,
        capture_output=True,
        check=True,
    )
    values: dict[str, str] = {}
    for line in details.stderr.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"CDHash", "TeamIdentifier"}:
            values[key] = value.strip()
        elif separator and key == "Authority" and "Developer ID Application" in value:
            values[key] = value.strip()
    plist = app / "Contents" / "Info.plist"
    with plist.open("rb") as handle:
        info = plistlib.load(handle)
    values["CFBundleVersion"] = str(info.get("CFBundleVersion", ""))
    values["CFBundleShortVersionString"] = str(
        info.get("CFBundleShortVersionString", "")
    )
    if values.get("TeamIdentifier") != EXPECTED_DESKTOP_TEAM:
        raise RuntimeError(
            f"unexpected Desktop signing team: {values.get('TeamIdentifier')}"
        )
    if "Developer ID Application" not in values.get("Authority", ""):
        raise RuntimeError(f"unexpected Desktop signing authority: {values.get('Authority')}")
    required = (
        "CDHash",
        "TeamIdentifier",
        "Authority",
        "CFBundleVersion",
        "CFBundleShortVersionString",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise RuntimeError(f"Desktop identity incomplete: {', '.join(missing)}")
    return values


def identity_matches(actual: dict[str, str], expected: dict[str, str]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def prune_desktop_backup_apps(
    applications_dir: Path,
    user_applications_dir: Path,
    *,
    keep: frozenset[Path] = frozenset(),
) -> list[Path]:
    """Remove obsolete Desktop rollback bundles while preserving this run's.

    A failed update keeps its rollback bundle for diagnosis. The next install
    replaces that recovery point, so older update-service and legacy manual
    backups only consume disk and must not accumulate indefinitely.
    """
    patterns = (
        (applications_dir, ".Hermes.update-prior-*.app"),
        (applications_dir, ".Hermes.app.old-*"),
        (applications_dir, ".Hermes.prior-dup-fix.app"),
        (applications_dir, ".Hermes.app.pre-update-smart"),
        (user_applications_dir, "Hermes.app.backup-*"),
    )
    preserved = {path.resolve() for path in keep}
    removed: list[Path] = []
    for root, pattern in patterns:
        if not root.is_dir():
            continue
        for path in root.glob(pattern):
            if path.resolve() in preserved:
                continue
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                continue
            removed.append(path)
    return removed


def swap_desktop_apps(staging: Path, installed: Path, prior: Path) -> None:
    if not staging.is_dir():
        raise RuntimeError("Desktop staging artifact missing")
    if installed.exists():
        if prior.exists():
            shutil.rmtree(installed)
        else:
            installed.rename(prior)
    staging.rename(installed)


def execute_worker(repo: Path, root: Path, run_id: str) -> None:
    lease_handle = None
    bundle = run_dir(root, run_id) / "bundle"
    worker_fd_baseline = fd_count(os.getpid())
    try:
        lease_handle = acquire_lease(root, run_id)
        reap_tracked_children(root, run_id)
        verify_bundle(bundle)
        ledger = read_json(ledger_path(root, run_id))
        if ledger["branch"] != "local/studio-slim":
            raise RuntimeError(f"unexpected live branch: {ledger['branch']}")
        base = str(ledger["base_commit"])
        ref = f"refs/hermes/update-runs/{run_id}"
        worktree = worktree_for(root, run_id)
        if phase_before(ledger, "MERGED"):
            if git(repo, "branch", "--show-current") != "local/studio-slim":
                raise RuntimeError("live checkout branch changed before merge")
            if git(repo, "status", "--porcelain"):
                raise RuntimeError("live checkout must be clean before merge")
            if git(repo, "rev-parse", "HEAD") != base:
                raise RuntimeError("live checkout base changed before merge")
            ensure_not_aborted(root, run_id)
            if ledger["phase"] == "CREATED":
                transition(root, run_id, "PREFLIGHT")
            ledger = read_json(ledger_path(root, run_id))
            upstream = str(ledger.get("upstream_sha") or "")
            if not upstream:
                worker_command(
                    root,
                    run_id,
                    ["git", "fetch", "origin", "main"],
                    repo,
                    "fetch",
                    300,
                )
                upstream = git(repo, "rev-parse", "origin/main")
                record(root, run_id, upstream_sha=upstream)
            if not read_json(ledger_path(root, run_id)).get("assessed_at"):
                created_at = dt.datetime.fromisoformat(
                    str(read_json(ledger_path(root, run_id))["created_at"])
                )
                assessment = preflight_assessment(
                    repo,
                    base,
                    upstream,
                    fast_path_started_at=created_at,
                )
                record(root, run_id, **assessment)
            worker_command(
                root,
                run_id,
                [
                    sys.executable,
                    str(bundle / "carry.py"),
                    "--root",
                    str(repo),
                    "--manifest",
                    str(bundle / "local_carry_manifest.yaml"),
                    "validate",
                    "--against",
                    upstream,
                ],
                repo,
                "carry-preflight",
                120,
            )
            ledger = read_json(ledger_path(root, run_id))
            checkpoint_ref = ledger.get("checkpoint_ref")
            if not checkpoint_ref:
                checkpoint = subprocess.run(
                    [
                        sys.executable,
                        str(bundle / "hermes_checkpoint.py"),
                        "--repo",
                        str(repo),
                        "snapshot",
                    ],
                    text=True,
                    capture_output=True,
                    preexec_fn=lambda: resource.setrlimit(
                        resource.RLIMIT_NOFILE,
                        (FD_LIMIT, FD_LIMIT),
                    ),
                )
                if checkpoint.returncode not in {0, 1}:
                    raise RuntimeError(f"checkpoint failed: {checkpoint.stdout[-500:]}")
                checkpoint_ref = json.loads(checkpoint.stdout).get("ref")
            if not git(repo, "rev-parse", "-q", "--verify", ref, check=False):
                git(repo, "update-ref", ref, base)
            worktree.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not worktree.exists():
                git(
                    repo,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    ref,
                )
            link_checkout_dependencies(repo, worktree)
            record(
                root,
                run_id,
                checkpoint_ref=checkpoint_ref,
                run_ref=ref,
            )
            ensure_not_aborted(root, run_id)
            merge_head = git(
                worktree,
                "rev-parse",
                "-q",
                "--verify",
                "MERGE_HEAD",
                check=False,
            )
            merge_rc = 0
            if git(worktree, "rev-parse", "HEAD") == base and not merge_head:
                merge_rc = worker_command(
                    root,
                    run_id,
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "merge",
                        "--no-edit",
                        upstream,
                    ],
                    worktree,
                    "merge",
                    1800,
                    allow_failure=True,
                )
            conflicts = git(worktree, "diff", "--name-only", "--diff-filter=U").splitlines()
            merge_in_progress = bool(
                git(
                    worktree,
                    "rev-parse",
                    "-q",
                    "--verify",
                    "MERGE_HEAD",
                    check=False,
                )
            )
            if merge_rc and not conflicts and not merge_in_progress:
                raise RuntimeError("merge failed without resolvable file conflicts")
            if conflicts:
                record(root, run_id, merge_conflicts=conflicts)
                resolver = shutil.which("hermes")
                ledger = read_json(ledger_path(root, run_id))
                if resolver and not ledger.get("resolver_attempted"):
                    record(root, run_id, resolver_attempted=True)
                    prompt = (
                        "Resolve only the listed Git merge conflicts in the supplied worktree. "
                        "Preserve upstream behavior and registered local carry. Do not run tests, "
                        "spawn subagents, background work, stage, commit, push, or restart anything. "
                        f"Worktree: {worktree}. Conflicts: {', '.join(conflicts)}. "
                        "Remove all conflict markers from those files and stop."
                    )
                    worker_command(
                        root,
                        run_id,
                        [
                            resolver,
                            "--profile",
                            "coding",
                            "--safe-mode",
                            "-t",
                            "file",
                            "-z",
                            prompt,
                        ],
                        worktree,
                        "conflict-worker",
                        3600,
                    )
                resolved = [
                    path
                    for path in conflicts
                    if conflict_is_resolved(worktree / path)
                ]
                if resolved:
                    worker_command(
                        root,
                        run_id,
                        ["git", "add", "--", *resolved],
                        worktree,
                        "conflict-stage",
                        120,
                    )
            remaining = git(
                worktree,
                "diff",
                "--name-only",
                "--diff-filter=U",
            ).splitlines()
            if remaining:
                transition(
                    root,
                    run_id,
                    PARKED,
                    error=(
                        "conflicts await resolution in the integration worktree; "
                        "resolve there, then issue the resume verb"
                    ),
                    conflict_files=remaining,
                    worktree=str(worktree),
                )
                return
            if git(
                worktree,
                "rev-parse",
                "-q",
                "--verify",
                "MERGE_HEAD",
                check=False,
            ):
                worker_command(
                    root,
                    run_id,
                    ["git", "-c", "core.hooksPath=/dev/null", "commit", "--no-edit"],
                    worktree,
                    "merge-commit",
                    300,
                )
            result_commit = git(worktree, "rev-parse", "HEAD")
            git(repo, "update-ref", ref, result_commit)
            transition(root, run_id, "MERGED", result_commit=result_commit)
        ledger = read_json(ledger_path(root, run_id))
        upstream = str(ledger["upstream_sha"])
        result_commit = str(ledger["result_commit"])
        if not worktree.exists():
            worktree.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            git(
                repo,
                "-c",
                "core.hooksPath=/dev/null",
                "worktree",
                "add",
                "--detach",
                str(worktree),
                ref,
            )
        link_checkout_dependencies(repo, worktree)
        changed = git(worktree, "diff", "--name-only", f"{base}...{result_commit}").splitlines()
        desktop_diff_changed = any(
            path.startswith("apps/desktop/") for path in changed
        )
        web_diff_changed = any(path.startswith("web/") for path in changed)
        dependency_sensitive = [
            path
            for path in changed
            if Path(path).name in DEP_MANIFEST_NAMES
            or Path(path).name.endswith(".lock")
        ]
        # Keep the Desktop build serialized behind validation.  Building and
        # signing Electron saturates this host enough to make the large TUI
        # gateway test file exceed its otherwise-generous timeout, creating a
        # false validation failure.  Reliability is worth more than overlap in
        # an unattended transactional update.
        desktop_build_child: subprocess.Popen[bytes] | None = None
        desktop_build_output: Any = None
        try:
            if phase_before(ledger, "VERIFIED"):
                ensure_not_aborted(root, run_id)
                if dependency_manifests_changed(changed):
                    materialize_node_dependencies(root, run_id, worktree)
                carry_env = os.environ.copy()
                carry_env["HERMES_CARRY_TEST_JOBS"] = "1"
                carry_env["HERMES_CARRY_PER_FEATURE"] = "1"
                worker_command(
                    root,
                    run_id,
                    [
                        sys.executable,
                        str(bundle / "carry.py"),
                        "--root",
                        str(worktree),
                        "--manifest",
                        str(bundle / "local_carry_manifest.yaml"),
                        "verify",
                        "--changed-since",
                        base,
                        "--skip-runtime-probes",
                        "--json",
                        str(run_dir(root, run_id) / "evidence" / "carry-verify.json"),
                    ],
                    worktree,
                    "carry-verify",
                    3600,
                    env=carry_env,
                    child_fd_limit=VALIDATION_FD_LIMIT,
                )
                validation_env = os.environ.copy()
                validation_env["HERMES_REPO_ROOT"] = str(worktree)
                validation_env["HERMES_TEST_RUNNER"] = str(bundle / "run_tests.sh")
                validation_env["UPDATE_CHANGED_DESKTOP"] = (
                    "1" if desktop_diff_changed or web_diff_changed else "0"
                )
                _gate_ledger = read_json(ledger_path(root, run_id))
                full_required, full_reason = full_validation_required(
                    _gate_ledger,
                    changed,
                    dependency_sensitive,
                )
                full_required, full_reason = effective_validation(
                    _gate_ledger, full_required, full_reason
                )
                if full_required:
                    validation_env["UPDATE_VALIDATION_FULL"] = "1"
                if full_reason:
                    record(root, run_id, full_validation_reason=full_reason)
                worker_command(
                    root,
                    run_id,
                    [str(bundle / "run_update_smart_client_validation.sh")],
                    worktree,
                    "curated-validation",
                    7200,
                    env=validation_env,
                    child_fd_limit=VALIDATION_FD_LIMIT,
                )
                transition(
                    root,
                    run_id,
                    "VERIFIED",
                    changed_paths=changed,
                    dependency_sensitive_paths=dependency_sensitive,
                )
                if desktop_diff_changed:
                    desktop_build_child, desktop_build_output = start_owned_child(
                        ["npm", "run", "dist:mac"],
                        worktree / "apps" / "desktop",
                        run_dir(root, run_id) / "evidence" / "desktop-build.log",
                        child_fd_limit=DESKTOP_BUILD_FD_LIMIT,
                    )
                    track_owned_child(
                        root, run_id, "desktop-build", desktop_build_child
                    )
            ledger = read_json(ledger_path(root, run_id))
            desktop_changed = bool(
                ledger.get("desktop_changed") or desktop_diff_changed
            )
            if phase_before(ledger, "BUILT"):
                desktop_artifact: str | None = None
                desktop_artifact_identity: dict[str, str] | None = None
                if desktop_changed:
                    if desktop_build_child is not None:
                        try:
                            wait_worker_child(
                                root,
                                run_id,
                                desktop_build_child,
                                desktop_build_output,
                                ["npm", "run", "dist:mac"],
                                7200,
                            )
                        finally:
                            untrack_owned_child(root, run_id, "desktop-build")
                        desktop_build_child = None
                    else:
                        worker_command(
                            root,
                            run_id,
                            ["npm", "run", "dist:mac"],
                            worktree / "apps" / "desktop",
                            "desktop-build",
                            7200,
                            child_fd_limit=DESKTOP_BUILD_FD_LIMIT,
                        )
                    apps = sorted(
                        (worktree / "apps" / "desktop" / "release").glob(
                            "**/Hermes.app"
                        ),
                        key=lambda path: path.stat().st_mtime_ns,
                    )
                    if not apps:
                        raise RuntimeError("signed Desktop artifact not found")
                    artifact = apps[-1]
                    desktop_artifact = str(artifact)
                    desktop_artifact_identity = desktop_identity(artifact)
                transition(
                    root,
                    run_id,
                    "BUILT",
                    desktop_changed=desktop_changed,
                    desktop_artifact=desktop_artifact,
                    desktop_artifact_identity=desktop_artifact_identity,
                )
        except BaseException:
            if desktop_build_child is not None:
                kill_owned_child(desktop_build_child, desktop_build_output)
                untrack_owned_child(root, run_id, "desktop-build")
            raise
        ledger = read_json(ledger_path(root, run_id))
        if ledger["mode"] == "rehearse":
            ensure_not_aborted(root, run_id)
            snapshot = enforce_budget(
                os.getpid(),
                int(ledger["origin_pid"]) if ledger.get("origin_pid") else None,
                max(FD_LIMIT, int(ledger.get("origin_fd_baseline") or 0) + 32),
            )
            if snapshot["fds"] > worker_fd_baseline + 8 or snapshot["descendants"]:
                raise RuntimeError(
                    "worker resources did not return to baseline: "
                    f"fds={snapshot['fds']} baseline={worker_fd_baseline} "
                    f"descendants={snapshot['descendants']}"
                )
            remove_rehearsal_worktree(repo, worktree)
            transition(
                root,
                run_id,
                "COMPLETED",
                completed_at=utc_now(),
                final_resources=snapshot,
                deployment="disabled-by-rehearsal",
            )
            return
        deploy(
            root,
            repo,
            worktree,
            run_id,
            ref,
            base,
            result_commit,
            desktop_changed,
            ledger.get("desktop_artifact"),
            ledger.get("desktop_artifact_identity"),
        )
    except LeaseHeld:
        return
    except AbortRequested as exc:
        current = read_json(ledger_path(root, run_id))
        if current.get("status") not in TERMINAL:
            transition(root, run_id, "ABORTED", error=str(exc))
    except Exception as exc:
        current = read_json(ledger_path(root, run_id))
        if current.get("status") not in TERMINAL:
            note = ""
            try:
                activated = phase_rank(str(current.get("phase", "CREATED"))) >= phase_rank(
                    "STUDIO_ACTIVATED"
                )
            except ValueError:
                activated = False
            if not activated and abort_requested(root, run_id):
                # The failure surfaced while an abort was pending: the abort is
                # the cause, mirror the explicit AbortRequested path above.
                transition(
                    root,
                    run_id,
                    "ABORTED",
                    error=f"aborted during {type(exc).__name__}",
                )
            else:
                if activated and current.get("dependency_sensitive_paths"):
                    note = (
                        " | rollback requires dependency review (git alone is insufficient): "
                        + ", ".join(current["dependency_sensitive_paths"][:10])
                    )
                transition(
                    root,
                    run_id,
                    "FAILED",
                    error=f"{type(exc).__name__}: {str(exc)[:500]}{note}",
                )
    finally:
        release_lease(lease_handle)


def consume_restart_outcome(root: Path, run_id: str, marker: Path) -> None:
    """Fail the restart wait immediately when the restart helper reported a
    failure through its completion marker instead of polling out the timeout.
    A missing or unreadable marker keeps the plain readiness-poll behavior."""
    if not marker.is_file():
        return
    try:
        outcome = read_json(marker)
    except (OSError, json.JSONDecodeError):
        return
    atomic_json(run_dir(root, run_id) / "evidence" / "restart-outcome.json", outcome)
    if int(outcome.get("exit_code") or 0) != 0:
        raise RuntimeError(
            "restart helper failed: "
            f"{outcome.get('message') or 'unknown'} "
            f"(log: {outcome.get('log_path') or 'unknown'})"
        )


def restart_busy_snapshot(repo: Path, scope: str = "hermes") -> dict[str, Any]:
    """Best-effort structured busy-state snapshot for drain-audit evidence.

    Shells into ``hermes_cli.restart_surfaces --busy-snapshot-json`` (cwd=repo,
    matching how restart-enqueue itself is invoked as ``-m hermes_cli.
    restart_surfaces``) instead of re-deriving active_agents/gateway_state/
    restart_requested/freshness math here — that module is the single source
    of truth for what "busy" means. Never raises: a diagnostic collection
    failure must not fail or block the drain wait that calls this.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.restart_surfaces",
                "--scope",
                scope,
                "--busy-snapshot-json",
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            return {
                "error": f"busy snapshot exited {result.returncode}: {result.stderr[-300:]}"
            }
        return json.loads(result.stdout)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def live_dependency_refresh(root: Path, run_id: str, repo: Path) -> None:
    """Refresh live dependency trees, then restore configured provider pins.

    The npm and uv installers target independent trees (node_modules vs .venv),
    so they overlap. Memory provider manifests are external to the uv project,
    and their resolved extras can vary by profile mode, so every configured
    profile is refreshed strictly after uv finishes. Any failure fails the
    phase with that command's error.
    """
    evidence = run_dir(root, run_id) / "evidence"
    # Load PyYAML before ``uv sync`` can replace the live virtualenv that this
    # worker itself is running from (for example when .python-version changes).
    # Keeping the imported module alive lets provider discovery continue after
    # the old site-packages directory has been removed.
    try:
        import yaml
    except Exception as exc:
        raise RuntimeError("failed to load memory-provider config parser") from exc

    jobs = (
        (["npm", "ci"], "live-npm-ci"),
        (["uv", "sync", *UV_SYNC_EXTRA_ARGS], "live-uv-sync"),
    )
    started: list[tuple[subprocess.Popen[bytes], Any, list[str], str]] = []
    try:
        for command_args, name in jobs:
            child, output = start_owned_child(
                command_args,
                repo,
                evidence / f"{name}.log",
                child_fd_limit=DESKTOP_BUILD_FD_LIMIT,
            )
            track_owned_child(root, run_id, name, child)
            started.append((child, output, command_args, name))
        for child, output, command_args, name in started:
            wait_worker_child(
                root,
                run_id,
                child,
                output,
                command_args,
                3600,
                honor_abort=False,
            )
            untrack_owned_child(root, run_id, name)

        hermes_root = repo.parent
        config_paths = [hermes_root / "config.yaml"]
        config_paths.extend(sorted((hermes_root / "profiles").glob("*/config.yaml")))
        provider_homes: list[tuple[str, Path]] = []
        try:
            for config_path in config_paths:
                if not config_path.is_file():
                    continue
                config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                memory = config.get("memory") if isinstance(config, dict) else None
                if not isinstance(memory, dict) or memory.get("enabled") is False:
                    continue
                provider = str(memory.get("provider") or "").strip()
                if provider and provider not in {"default", "builtin", "none"}:
                    provider_homes.append((provider, config_path.parent))
        except Exception as exc:
            raise RuntimeError("failed to discover configured memory providers") from exc

        refresh_code = (
            "from hermes_cli.update_cmd import "
            "_refresh_active_memory_provider_dependencies as refresh; "
            "refresh(strict=True)"
        )
        for provider, profile_home in provider_homes:
            safe_provider = re.sub(r"[^A-Za-z0-9_.-]+", "-", provider)
            safe_profile = re.sub(r"[^A-Za-z0-9_.-]+", "-", profile_home.name)
            env = os.environ.copy()
            env["HERMES_HOME"] = str(profile_home)
            env["PYTHONPATH"] = str(repo)
            worker_command(
                root,
                run_id,
                [str(repo / ".venv" / "bin" / "python"), "-c", refresh_code],
                repo,
                f"live-memory-provider-{safe_profile}-{safe_provider}",
                300,
                env=env,
                honor_abort=False,
            )
    except BaseException:
        for child, output, _, name in started:
            kill_owned_child(child, output)
            untrack_owned_child(root, run_id, name)
        raise


def deploy(
    root: Path,
    repo: Path,
    worktree: Path,
    run_id: str,
    ref: str,
    base: str,
    result_commit: str,
    desktop_changed: bool,
    desktop_artifact: str | None,
    desktop_artifact_identity: dict[str, str] | None,
) -> None:
    remote_ref = f"refs/hermes/update-runs/{run_id}"
    stage_key = hashlib.sha256(f"{run_id}:{result_commit}:macbook-stage".encode()).hexdigest()

    def macbook_reachable() -> bool:
        return (
            subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=8",
                    "macbook",
                    "true",
                ],
                capture_output=True,
            ).returncode
            == 0
        )

    def stage_probe() -> tuple[bool, dict[str, Any]]:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                "macbook",
                f"git -C ~/.hermes/hermes-agent rev-parse {remote_ref}",
            ],
            text=True,
            capture_output=True,
        )
        observed = result.stdout.strip()
        return result.returncode == 0 and observed == result_commit, {"commit": observed}

    def stage_action() -> None:
        honor_abort = phase_before(
            read_json(ledger_path(root, run_id)),
            "STUDIO_ACTIVATED",
        )
        worker_command(
            root,
            run_id,
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "push",
                "ildunari",
                f"{ref}:{remote_ref}",
            ],
            repo,
            "push-run-ref",
            300,
            honor_abort=honor_abort,
        )
        remote = (
            "set -e; cd ~/.hermes/hermes-agent; "
            "test \"$(git branch --show-current)\" = local/studio-slim; "
            "test -z \"$(git status --porcelain)\"; "
            f"git fetch ildunari {remote_ref}:{remote_ref}; "
            f"test \"$(git rev-parse {remote_ref})\" = {result_commit}"
        )
        worker_command(
            root,
            run_id,
            ["ssh", "macbook", remote],
            repo,
            "macbook-stage",
            600,
            honor_abort=honor_abort,
        )

    # One bounded reachability probe (ConnectTimeout=8) decides staging vs
    # deferral. An unreachable travel MacBook defers instead of failing the
    # Studio update; the deferral is recorded and the remote gateway check is
    # skipped during runtime verification. Never retry-loop, wake, or mutate
    # an absent MacBook merely to close the local run.
    if receipt_path(root, run_id, "macbook_stage").is_file() or macbook_reachable():
        receipted(
            root,
            run_id,
            "macbook_stage",
            stage_key,
            {"commit": result_commit},
            stage_probe,
            stage_action,
            lambda: abort_before_activation(root, run_id),
        )
        current_ledger = read_json(ledger_path(root, run_id))
        if current_ledger.get("macbook_deferred"):
            # A run deferred while offline can stage on recovery once the
            # MacBook is back; the stale deferral flag must not keep runtime
            # verification skipping the remote check.
            transition(
                root,
                run_id,
                str(current_ledger["phase"]),
                macbook_deferred=False,
                macbook_deferred_reason=None,
            )
        advance(root, run_id, "MACBOOK_STAGED")
    else:
        advance(
            root,
            run_id,
            "MACBOOK_STAGED",
            macbook_deferred=True,
            macbook_deferred_reason="macbook ssh unreachable at stage time",
        )
    activate_key = hashlib.sha256(f"{run_id}:{result_commit}:studio-activate".encode()).hexdigest()

    def activate_probe() -> tuple[bool, dict[str, Any]]:
        current = git(repo, "rev-parse", "HEAD")
        return current == result_commit, {"commit": current}

    def activate_action() -> None:
        if git(repo, "branch", "--show-current") != "local/studio-slim":
            raise RuntimeError("live checkout branch changed")
        if git(repo, "status", "--porcelain"):
            raise RuntimeError("live checkout became dirty")
        if git(repo, "rev-parse", "HEAD") != base:
            raise RuntimeError("live checkout base changed")
        worker_command(
            root,
            run_id,
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "merge",
                "--ff-only",
                result_commit,
            ],
            repo,
            "studio-activate",
            300,
            honor_abort=False,
        )

    receipted(
        root,
        run_id,
        "studio_activate",
        activate_key,
        {"commit": result_commit},
        activate_probe,
        activate_action,
        lambda: abort_before_activation(root, run_id),
    )
    advance(root, run_id, "STUDIO_ACTIVATED")
    ledger_now = read_json(ledger_path(root, run_id))
    if ledger_now.get("dependency_sensitive_paths"):
        # The worktree got its own npm ci during validation, but activation
        # fast-forwards the LIVE checkout, whose node_modules and .venv still
        # match the old lockfiles. Refresh them before the restart so the
        # relaunched services never pair new code with stale dependencies
        # (run 20260723T162811Z failed deployed carry-verify on exactly this).
        refresh_key = hashlib.sha256(
            f"{run_id}:{result_commit}:live-deps".encode()
        ).hexdigest()

        receipt = prepare_receipt(
            root,
            run_id,
            "live_dependency_refresh",
            refresh_key,
            {"commit": result_commit},
        )
        if receipt.get("state") != "COMPLETED":
            live_dependency_refresh(root, run_id, repo)
            complete_receipt(
                root, run_id, "live_dependency_refresh", {"refreshed": True}
            )
    install_key = hashlib.sha256(f"{run_id}:{result_commit}:artifact".encode()).hexdigest()
    expected_identity = desktop_artifact_identity or {}
    prior = Path(f"/Applications/.Hermes.update-prior-{run_id}.app")

    def install_probe() -> tuple[bool, dict[str, Any]]:
        if not desktop_changed:
            return True, {"desktop": "unchanged"}
        app = Path("/Applications/Hermes.app")
        try:
            observed = desktop_identity(app)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            return False, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        return identity_matches(observed, expected_identity), observed

    def install_action() -> None:
        if not desktop_changed:
            return
        if not desktop_artifact:
            raise RuntimeError("pinned Desktop artifact path missing")
        artifact = Path(desktop_artifact)
        if not artifact.is_dir():
            raise RuntimeError("pinned Desktop artifact missing")
        actual = desktop_identity(artifact)
        if not identity_matches(actual, expected_identity):
            raise RuntimeError("Desktop artifact identity changed after build")
        prune_desktop_backup_apps(
            Path("/Applications"),
            Path.home() / "Applications",
            keep=frozenset({prior}),
        )
        staging = Path(f"/Applications/.Hermes.update-staging-{run_id}.app")
        if staging.exists():
            shutil.rmtree(staging)
        worker_command(
            root,
            run_id,
            ["ditto", str(artifact), str(staging)],
            worktree,
            "artifact-copy",
            600,
            honor_abort=False,
        )
        installed = Path("/Applications/Hermes.app")
        swap_desktop_apps(staging, installed, prior)

    receipted(
        root,
        run_id,
        "artifact_install",
        install_key,
        {
            "commit": result_commit,
            "desktop_changed": desktop_changed,
            "identity": expected_identity,
        },
        install_probe,
        install_action,
    )
    advance(root, run_id, "ARTIFACT_INSTALLED")
    restart_key = hashlib.sha256(f"{run_id}:{result_commit}:restart".encode()).hexdigest()
    existing_restart = receipt_path(root, run_id, "restart")
    if existing_restart.is_file():
        raw_old_pids = read_json(existing_restart).get("expected", {}).get(
            "old_listener_pids", {}
        )
        old_listener_pids = {int(key): int(value) for key, value in raw_old_pids.items()}
    else:
        old_listener_pids = listener_pids()
    restart_outcome_marker = run_dir(root, run_id) / "receipts" / "restart-outcome.json"

    def restart_probe() -> tuple[bool, dict[str, Any]]:
        consume_restart_outcome(root, run_id, restart_outcome_marker)
        return surface_inventory(old_listener_pids)

    def restart_action() -> None:
        # A worker that died while waiting out the drain must not enqueue a
        # second restart on recovery: the marker records the one enqueue and
        # recovery only re-enters the wait.
        enqueue_marker = run_dir(root, run_id) / "receipts" / "restart-enqueued.json"
        if not enqueue_marker.is_file():
            worker_command(
                root,
                run_id,
                [
                    sys.executable,
                    "-m",
                    "hermes_cli.restart_surfaces",
                    "--scope",
                    "hermes",
                    "--delay",
                    "10",
                    "--safe-wait-timeout",
                    "86400",
                    "--enqueue-detached",
                    "--completion-marker",
                    str(restart_outcome_marker),
                ],
                repo,
                "restart-enqueue",
                120,
                honor_abort=False,
            )
            enqueue_marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_json(enqueue_marker, {"enqueued_at": utc_now()})
        wait_for_surfaces(
            root,
            run_id,
            restart_probe,
            RESTART_WAIT_SECONDS,
            busy_probe=lambda: restart_busy_snapshot(repo),
        )

    receipted(
        root,
        run_id,
        "restart",
        restart_key,
        {"commit": result_commit, "old_listener_pids": old_listener_pids},
        restart_probe,
        restart_action,
    )
    advance(root, run_id, "RESTARTED")
    verify_key = hashlib.sha256(f"{run_id}:{result_commit}:runtime".encode()).hexdigest()

    turn_marker = run_dir(root, run_id) / "receipts" / "authenticated-turn.json"

    def runtime_probe() -> tuple[bool, dict[str, Any]]:
        current = git(repo, "rev-parse", "HEAD")
        health, details = surface_inventory(old_listener_pids)
        deferred = bool(
            read_json(ledger_path(root, run_id)).get("macbook_deferred")
        )
        if deferred:
            remote_ok = True
            remote_state: Any = "deferred"
        else:
            remote = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=8",
                    "macbook",
                    "for p in 8642 8787; do "
                    "test -z \"$(/usr/sbin/lsof -nP -iTCP:$p -sTCP:LISTEN -t)\" "
                    "|| exit 9; done",
                ],
                text=True,
                capture_output=True,
            )
            remote_ok = remote.returncode == 0
            remote_state = remote_ok
        return (
            current == result_commit
            and health
            and remote_ok
            and turn_marker.is_file(),
            {
                "commit": current,
                "surfaces": details,
                "macbook_gateway_stopped": remote_state,
                "authenticated_turn": turn_marker.is_file(),
            },
        )

    def runtime_action() -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(run_dir(root, run_id) / "bundle" / "verify_deployed_turn.py"),
                "--run-id",
                run_id,
                "--expected-commit",
                result_commit,
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            preexec_fn=lambda: resource.setrlimit(
                resource.RLIMIT_NOFILE,
                (FD_LIMIT, FD_LIMIT),
            ),
        )
        if result.returncode:
            raise RuntimeError("authenticated representative turn failed")
        payload = json.loads(result.stdout)
        worker_command(
            root,
            run_id,
            [
                sys.executable,
                str(run_dir(root, run_id) / "bundle" / "carry.py"),
                "--root",
                str(repo),
                "--manifest",
                str(run_dir(root, run_id) / "bundle" / "local_carry_manifest.yaml"),
                "verify",
                "--changed-since",
                base,
                # The worktree already ran the behavior tests on this exact
                # commit; the deployed pass only proves runtime wiring.
                "--probes-only",
                "--json",
                str(run_dir(root, run_id) / "evidence" / "deployed-carry-verify.json"),
            ],
            repo,
            "deployed-carry-verify",
            3600,
            honor_abort=False,
        )
        atomic_json(turn_marker, payload)

    receipted(
        root,
        run_id,
        "runtime_verify",
        verify_key,
        {"commit": result_commit},
        runtime_probe,
        runtime_action,
    )
    advance(root, run_id, "RUNTIME_VERIFIED")
    if prior.exists():
        shutil.rmtree(prior)
    worktree = worktree_for(root, run_id)
    if worktree.exists():
        # The integration worktree is only needed until activation; leaving it
        # accumulates full checkouts and trips the checklist's
        # interrupted-update detection.
        git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        git(repo, "worktree", "prune", check=False)
    transition(root, run_id, "COMPLETED", completed_at=utc_now())


def run_worker(args: argparse.Namespace) -> int:
    execute_worker(args.repo.resolve(), state_root(args.state_root), args.run_id)
    return 0


# A run's status stays FAILED/ABORTED forever once terminal (see TERMINAL and
# transition()'s guard); retire only ever acts on those two, never COMPLETED
# (which already self-cleans its worktree at the end of deploy()) and never a
# still-active run.
RETIRABLE_STATUSES = {"FAILED", "ABORTED"}
# The only fields stamp_terminal_ledger may set on an immutable terminal
# ledger — retirement bookkeeping, never status/phase/run_id/history.
_RETIRE_LEDGER_FIELDS = frozenset(
    {
        "retired",
        "retired_at",
        "retire_worktree_removed",
        "retire_archived_paths",
        "retire_residue_removed",
    }
)


def resolve_last_failed_run(root: Path) -> str | None:
    """Most recent run whose ledger status is FAILED, newest first.

    Mirrors active_run()'s reverse-sorted scan (run ids are zero-padded UTC
    timestamps, so lexicographic order is chronological order).
    """
    runs = root / "runs"
    if not runs.is_dir():
        return None
    for path in sorted(runs.glob("*/ledger.json"), reverse=True):
        try:
            ledger = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        # Skip already-retired runs: otherwise the newest retired failure
        # permanently shadows older un-retired ones (Codex batch-2 review P2).
        if ledger.get("status") == "FAILED" and not ledger.get("retired"):
            return str(ledger["run_id"])
    return None


def worktree_unique_commits(
    repo: Path, worktree: Path, run_ref: str | None
) -> list[str]:
    """Commits reachable only from this run's ref/worktree, not from any local
    branch or remote-tracking ref.

    ``git worktree remove --force`` (and losing the ref alongside it) would
    make these commits unreachable and effectively lost, so retire must
    refuse whenever this list is non-empty rather than guess which side to
    keep.
    """
    # Check BOTH the run ref AND the live worktree HEAD: the ref can be stale
    # (a parked run resolved by hand advances HEAD without moving run_ref), so
    # trusting the ref alone could miss real unique commits (Codex batch-2
    # review P1-6). Take the union of commits unique to either tip.
    tips: list[str] = []
    if run_ref:
        ref_tip = git(repo, "rev-parse", "-q", "--verify", run_ref, check=False)
        if ref_tip:
            tips.append(ref_tip)
    if worktree.exists():
        head_tip = git(worktree, "rev-parse", "-q", "--verify", "HEAD", check=False)
        if head_tip and head_tip not in tips:
            tips.append(head_tip)
    if not tips:
        return []
    other_refs = [
        line
        for line in git(
            repo, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"
        ).splitlines()
        if line and line != run_ref
    ]
    unique: list[str] = []
    seen: set[str] = set()
    for tip in tips:
        output = (
            tip
            if not other_refs
            else git(repo, "rev-list", tip, "--not", *other_refs, check=False)
        )
        for line in output.splitlines():
            if line and line not in seen:
                seen.add(line)
                unique.append(line)
    return unique


def worktree_dirty_paths(worktree: Path) -> list[str]:
    """Return modified/untracked paths in the worktree (empty when clean).

    ``git worktree remove --force`` deletes uncommitted resolutions and
    untracked files without a trace, so retire must refuse when this is
    non-empty unless the caller has archived a full patch (Codex batch-2
    review P1-6). Uses ``status --porcelain`` so both staged and untracked
    content is reported.

    ``.update-smart-validation`` is EXCLUDED: it is the validation script's
    own scratch dir, always untracked, and retire explicitly archives it via
    archive_run_evidence() before removal — refusing on it would block every
    legitimate retire. Any OTHER dirty/untracked path is real, unarchived
    work and blocks the removal.
    """
    if not worktree.exists():
        return []
    output = git(worktree, "status", "--porcelain", check=False)
    dirty = []
    for line in output.splitlines():
        if not line.strip():
            continue
        # porcelain format: "XY <path>"; path starts at column 3.
        rel = line[3:].strip().strip('"')
        if rel == ".update-smart-validation" or rel.startswith(".update-smart-validation/"):
            continue
        dirty.append(line)
    return dirty


def archive_run_evidence(root: Path, run_id: str, worktree: Path) -> list[str]:
    """Copy residue that lives outside the run's own ledger directory into it
    before the worktree is deleted.

    Everything the worker itself produces (worker_command logs, receipts,
    drain-audit, carry-verify JSON) already lands under run_dir() and is
    already durable — this only rescues the validation script's own scratch
    directory, which the curated-validation step writes inside the worktree
    (scripts/run_update_smart_client_validation.sh's $LOGDIR), not under
    run_dir(), so it would otherwise vanish with the worktree.
    """
    archived: list[str] = []
    residue = worktree / ".update-smart-validation"
    if residue.is_dir():
        destination = run_dir(root, run_id) / "archive" / "update-smart-validation"
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(residue, destination)
        archived.append(str(destination))
    return archived


def stamp_terminal_ledger(root: Path, run_id: str, **updates: Any) -> dict[str, Any]:
    """Attach bookkeeping to an already-terminal ledger.

    transition()/record() deliberately refuse to touch a terminal ledger — a
    finished run's record must not silently change. Retiring a terminal run
    is bookkeeping, not a phase/status change, so this takes the same file
    lock and writes directly instead of going through transition().
    """
    # Retirement bookkeeping only: never let this immutability exception be
    # used to rewrite status/phase/run_id/history (Codex batch-2 review P2).
    disallowed = set(updates) - _RETIRE_LEDGER_FIELDS
    if disallowed:
        raise RuntimeError(
            f"stamp_terminal_ledger refuses non-retirement fields: {sorted(disallowed)}"
        )
    path = ledger_path(root, run_id)
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = read_json(path)
        if str(ledger.get("status")) not in TERMINAL:
            raise RuntimeError(f"run is not terminal: {ledger.get('status')}")
        ledger.update(updates)
        atomic_json(path, ledger)
        return ledger


def retire_run(repo: Path, root: Path, run_id: str) -> dict[str, Any]:
    """Clean up a terminal (failed/aborted) run so the update preflight
    checklist stops seeing it as an interrupted, still-live run.

    Archives evidence that would otherwise be lost, removes the integration
    worktree (refusing when it holds commits unreachable from any branch or
    remote), removes leftover .update-smart-validation residue, and stamps
    the ledger retired. Refuses outright on a non-terminal run.
    """
    ledger = read_json(ledger_path(root, run_id))
    status = str(ledger.get("status"))
    if status not in RETIRABLE_STATUSES:
        raise RuntimeError(
            f"run {run_id} is not retirable: status={status} "
            f"(only {sorted(RETIRABLE_STATUSES)} can be retired)"
        )
    if ledger.get("retired"):
        return {"run_id": run_id, "already_retired": True}
    lease = current_lease(root)
    if lease and str(lease.get("run_id")) == run_id and lease_is_live(root):
        raise RuntimeError(f"run {run_id} has a live worker lease; wait for it to finish")

    worktree = worktree_for(root, run_id)
    run_ref = ledger.get("run_ref")
    unique = worktree_unique_commits(repo, worktree, run_ref)
    if unique:
        raise RuntimeError(
            f"refusing to remove worktree for {run_id}: "
            f"{len(unique)} commit(s) not reachable from any branch or remote "
            f"({', '.join(unique[:5])}{', ...' if len(unique) > 5 else ''})"
        )
    dirty = worktree_dirty_paths(worktree)
    if dirty:
        raise RuntimeError(
            f"refusing to remove worktree for {run_id}: it has "
            f"{len(dirty)} uncommitted/untracked path(s) that --force removal "
            f"would destroy ({', '.join(p[3:] for p in dirty[:5])}"
            f"{', ...' if len(dirty) > 5 else ''}). Commit, stash, or archive "
            "them first, then retire."
        )

    archived = archive_run_evidence(root, run_id, worktree)

    worktree_removed = False
    if worktree.exists():
        git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        git(repo, "worktree", "prune", check=False)
        if worktree.exists():
            raise RuntimeError(f"worktree remove did not clean up: {worktree}")
        worktree_removed = True

    # Defensive: the worktree removal above already deletes everything under
    # it, including .update-smart-validation, but cover the case where a
    # prior half-finished cleanup already removed the worktree by hand and
    # left the scratch directory orphaned at the same path.
    residue_removed = False
    residue = worktree / ".update-smart-validation"
    if residue.exists():
        shutil.rmtree(residue)
        residue_removed = True

    stamp_terminal_ledger(
        root,
        run_id,
        retired=True,
        retired_at=utc_now(),
        retire_archived_paths=archived,
        retire_worktree_removed=worktree_removed,
    )
    return {
        "run_id": run_id,
        "archived": archived,
        "worktree_removed": worktree_removed,
        "residue_removed": residue_removed,
    }


def run_retire(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    root = state_root(args.state_root)
    run_id = args.run_id
    if args.last_failed:
        if run_id:
            print("specify either --run-id or --last-failed, not both", file=sys.stderr)
            return 2
        run_id = resolve_last_failed_run(root)
        if not run_id:
            print("no FAILED run found to retire", file=sys.stderr)
            return 1
    if not run_id:
        print("retire requires --run-id or --last-failed", file=sys.stderr)
        return 2
    if not RUN_ID_RE.fullmatch(run_id):
        print(f"invalid run id: {run_id}", file=sys.stderr)
        return 2
    try:
        result = retire_run(repo, root, run_id)
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=DESCRIPTION)
    result.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument("--state-root", type=Path)
    sub = result.add_subparsers(dest="command", required=True)
    serving = sub.add_parser("serve")
    serving.set_defaults(func=serve)
    requesting = sub.add_parser("request")
    requesting.add_argument("verb", choices=("start", "status", "abort", "resume"))
    requesting.add_argument("--mode", choices=("rehearse", "update"), default="rehearse")
    requesting.add_argument("--run-id")
    requesting.add_argument(
        "--curated-override",
        dest="curated_override",
        help="Operator-inspected reason to keep this run on the curated lane "
        "despite a full-suite escalation (recorded in the ledger).",
    )
    requesting.add_argument(
        "--pin-upstream",
        dest="pin_upstream",
        help="Merge exactly this upstream commit (full 40-hex sha, already "
        "fetched) instead of fetching origin/main head.",
    )
    requesting.set_defaults(func=request)
    worker = sub.add_parser("run-worker")
    worker.add_argument("--run-id", required=True)
    worker.set_defaults(func=run_worker)
    retiring = sub.add_parser("retire")
    retiring.add_argument("--run-id")
    retiring.add_argument("--last-failed", action="store_true")
    retiring.set_defaults(func=run_retire)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
