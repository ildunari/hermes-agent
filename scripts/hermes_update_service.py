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
REQUEST_TTL = 60
FD_LIMIT = 256
DESKTOP_BUILD_FD_LIMIT = 2048
DESCENDANT_LIMIT = 64
WORKER_LIMIT = 2
REQUIRED_PORTS = (8642, 8787, 9119, 9120)
# Best-effort surfaces are probed and recorded but never gate readiness or
# fail the run. Keep the required/best-effort split explicit here; moving a
# port between the two tuples is a deliberate policy change, not a tweak.
BEST_EFFORT_PORTS: tuple[int, ...] = ()
RESTART_WAIT_SECONDS = 7200
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


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


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
    for port in REQUIRED_PORTS:
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
        for port in REQUIRED_PORTS
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
) -> None:
    """Poll readiness with an observable drain audit.

    While waiting, append a per-interval snapshot (listener PIDs, readiness,
    replacement state) to evidence/drain-audit.jsonl so a long drain is
    diagnosable instead of a silent hang. On timeout, name the blocked ports.
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
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"at": utc_now(), **details}, sort_keys=True) + "\n"
                )
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
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log.open("ab", buffering=0) as output:
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
        deadline = time.monotonic() + timeout
        while child.poll() is None:
            if abort_path and abort_path.exists():
                os.killpg(child.pid, signal.SIGTERM)
                time.sleep(1)
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
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
            raise subprocess.CalledProcessError(child.returncode, command_args)
        return int(child.returncode or 0)


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


def new_run(repo: Path, root: Path, mode: str, origin_pid: int | None) -> str:
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
    ledger = read_json(ledger_path(root, run_id))
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
    if set(payload) - {"verb", "mode", "run_id", "nonce", "timestamp"}:
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
        if set(payload) - {"verb", "mode", "nonce", "timestamp"}:
            raise ValueError("start payload is invalid")
        mode = payload.get("mode")
        if mode not in {"rehearse", "update"}:
            raise ValueError("invalid mode")
        prior = active_run(root)
        if prior:
            raise RuntimeError(f"run already active: {prior}")
        run_id = new_run(repo, root, mode, peer_pid)
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
        lease = current_lease(root)
        if lease:
            pid = int(lease.get("pid") or 0)
            expected_start = float(lease.get("start_time") or 0)
            if pid > 0 and pid != os.getpid() and ps_start_time(pid) == expected_start:
                raise RuntimeError(f"worker already live: pid {pid}")
        spawn_worker(repo, root, run_id)
        return {"ok": True, "run_id": run_id, "resumed": True}
    abort = run_dir(root, run_id) / "abort.request"
    abort.touch(mode=0o600, exist_ok=True)
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


def request(args: argparse.Namespace) -> int:
    root = state_root(args.state_root)
    payload: dict[str, Any] = {
        "verb": args.verb,
        "nonce": uuid.uuid4().hex,
        "timestamp": int(time.time()),
    }
    if args.verb == "start":
        payload["mode"] = args.mode
    else:
        payload["run_id"] = args.run_id
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(root / "update.sock"))
    client.sendall(json.dumps(payload).encode("utf-8"))
    response = client.recv(MAX_PAYLOAD).decode("utf-8")
    print(response.strip())
    return 0 if json.loads(response).get("ok") else 1


def current_lease(root: Path) -> dict[str, Any] | None:
    path = root / "lease.json"
    if not path.is_file():
        return None
    return read_json(path)


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
    ledger = read_json(ledger_path(root, run_id))
    origin_pid = ledger.get("origin_pid")
    baseline = int(ledger.get("origin_fd_baseline") or 0)
    origin_limit = max(FD_LIMIT, baseline + 32)
    return owned_command(
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


def abort_requested(root: Path, run_id: str) -> bool:
    return (run_dir(root, run_id) / "abort.request").exists()


def ensure_not_aborted(root: Path, run_id: str) -> None:
    if abort_requested(root, run_id):
        raise AbortRequested("abort requested")


def abort_before_activation(root: Path, run_id: str) -> None:
    ledger = read_json(ledger_path(root, run_id))
    if phase_before(ledger, "STUDIO_ACTIVATED"):
        ensure_not_aborted(root, run_id)


def worktree_for(root: Path, run_id: str) -> Path:
    return root / "worktrees" / run_id


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
                transition(root, run_id, "PREFLIGHT", upstream_sha=upstream)
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
            transition(
                root,
                run_id,
                "PREFLIGHT",
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
                resolver = shutil.which("hermes")
                ledger = read_json(ledger_path(root, run_id))
                if resolver and not ledger.get("resolver_attempted"):
                    transition(root, run_id, "PREFLIGHT", resolver_attempted=True)
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
                    if not CONFLICT_MARKER_RE.search(
                        (worktree / path).read_text(
                            encoding="utf-8",
                            errors="replace",
                        )
                    )
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
        if phase_before(ledger, "VERIFIED"):
            ensure_not_aborted(root, run_id)
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
            )
            validation_env = os.environ.copy()
            validation_env["HERMES_REPO_ROOT"] = str(worktree)
            validation_env["HERMES_TEST_RUNNER"] = str(bundle / "run_tests.sh")
            worker_command(
                root,
                run_id,
                [str(bundle / "run_update_smart_client_validation.sh")],
                worktree,
                "curated-validation",
                7200,
                validation_env,
            )
            dependency_sensitive = [
                path
                for path in changed
                if Path(path).name in DEP_MANIFEST_NAMES
                or Path(path).name.endswith(".lock")
            ]
            transition(
                root,
                run_id,
                "VERIFIED",
                changed_paths=changed,
                dependency_sensitive_paths=dependency_sensitive,
            )
        ledger = read_json(ledger_path(root, run_id))
        desktop_changed = bool(
            ledger.get("desktop_changed")
            or any(path.startswith("apps/desktop/") for path in changed)
        )
        if phase_before(ledger, "BUILT"):
            desktop_artifact: str | None = None
            desktop_artifact_identity: dict[str, str] | None = None
            if desktop_changed:
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

    # An unreachable travel MacBook defers staging instead of failing the
    # Studio update; the deferral is recorded and the remote gateway check is
    # skipped during runtime verification. Never probe/wake/mutate a travel
    # MacBook merely to close the local run.
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

    def restart_probe() -> tuple[bool, dict[str, Any]]:
        return surface_inventory(old_listener_pids)

    def restart_action() -> None:
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
            ],
            repo,
            "restart-enqueue",
            120,
            honor_abort=False,
        )
        wait_for_surfaces(root, run_id, restart_probe, RESTART_WAIT_SECONDS)

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
    transition(root, run_id, "COMPLETED", completed_at=utc_now())


def run_worker(args: argparse.Namespace) -> int:
    execute_worker(args.repo.resolve(), state_root(args.state_root), args.run_id)
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
    requesting.set_defaults(func=request)
    worker = sub.add_parser("run-worker")
    worker.add_argument("--run-id", required=True)
    worker.set_defaults(func=run_worker)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
