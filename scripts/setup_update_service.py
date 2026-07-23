#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


DESCRIPTION = "Install or verify the user-private Hermes update launch service."
LABEL = "com.ildunari.hermes-update-service"
NOTARY_PROFILE = "my-notary-profile"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    value.update(path.read_bytes())
    return value.hexdigest()


def active_run(state: Path) -> str | None:
    runs = state / "runs"
    if not runs.is_dir():
        return None
    for ledger in sorted(runs.glob("*/ledger.json"), reverse=True):
        try:
            payload = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # NEEDS_RESOLUTION is parked, not terminal: a parked run must block
        # updater reinstalls the same way a running one does.
        if payload.get("status") not in {"COMPLETED", "FAILED", "ABORTED"}:
            return str(payload.get("run_id"))
    return None


def plist_payload(python: str, service: Path, repo: Path, state: Path) -> dict[str, object]:
    logs = state / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python,
            str(service),
            "--repo",
            str(repo),
            "--state-root",
            str(state),
            "serve",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "APPLE_NOTARY_PROFILE": NOTARY_PROFILE,
        },
        "StandardOutPath": str(logs / "service.stdout.log"),
        "StandardErrorPath": str(logs / "service.stderr.log"),
        "ThrottleInterval": 5,
    }


def install(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    state = args.state.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    running = active_run(state)
    if running:
        print(f"refusing updater activation while run is non-terminal: {running}", file=sys.stderr)
        return 2
    source = repo / "scripts" / "hermes_update_service.py"
    version = digest(source)
    version_dir = state / "versions" / version
    version_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    installed = version_dir / source.name
    if installed.is_file():
        if digest(installed) != version:
            raise RuntimeError("existing update-service version is immutable but mismatched")
    else:
        shutil.copy2(source, installed)
    os.chmod(installed, 0o500)
    metadata = state / "active-version.json"
    metadata.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": version,
                "service": str(installed),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(metadata, 0o600)
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    with plist.open("wb") as handle:
        plistlib.dump(plist_payload(sys.executable, installed, repo, state), handle)
    os.chmod(plist, 0o600)
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    socket_path = state / "update.sock"
    socket_path.unlink(missing_ok=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if socket_path.is_socket():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1)
            try:
                probe.connect(str(socket_path))
                request = {
                    "verb": "status",
                    "run_id": "20000101T000000Z-000000000000",
                    "nonce": os.urandom(16).hex(),
                    "timestamp": int(time.time()),
                }
                probe.sendall(json.dumps(request).encode("utf-8"))
                ready = bool(probe.recv(4096))
            except OSError:
                ready = False
            finally:
                probe.close()
            if ready:
                break
        time.sleep(0.25)
    if not socket_path.is_socket():
        print("update service socket did not appear", file=sys.stderr)
        return 1
    print(f"update service installed: {LABEL} version={version}")
    return 0


def check(args: argparse.Namespace) -> int:
    state = args.state.resolve()
    source = args.repo.resolve() / "scripts" / "hermes_update_service.py"
    metadata = state / "active-version.json"
    socket_path = state / "update.sock"
    failures: list[str] = []
    if not metadata.is_file():
        failures.append("active-version.json missing")
    else:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        service = Path(str(payload.get("service", "")))
        if not service.is_file():
            failures.append("installed service missing")
        elif digest(service) != payload.get("sha256"):
            failures.append("installed service hash mismatch")
        elif not source.is_file() or digest(source) != payload.get("sha256"):
            failures.append("installed service does not match the tracked source")
    if not socket_path.is_socket():
        failures.append("private update socket missing")
    elif socket_path.stat().st_mode & 0o077:
        failures.append("private update socket permissions are too broad")
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        failures.append("launchd service is not loaded")
    if failures:
        print("Update service: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Update service: PASS")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=DESCRIPTION)
    result.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument(
        "--state",
        type=Path,
        default=Path.home() / ".hermes" / "update-service",
    )
    sub = result.add_subparsers(dest="command", required=True)
    installing = sub.add_parser("install")
    installing.set_defaults(func=install)
    checking = sub.add_parser("check")
    checking.set_defaults(func=check)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
