#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

LABEL = "ai.hermes.cron-guest"
DESCRIPTION = "Manage the profile-isolated Guest cron ticker LaunchAgent"
INTERVAL_SECONDS = 60
RUN_TIMEOUT_SECONDS = 50
PLIST_NAME = f"{LABEL}.plist"
LaunchctlRunner = Callable[..., subprocess.CompletedProcess[str]]


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def validate_runtime_paths(
    *, hermes_executable: str | Path, checkout: str | Path, profile_home: str | Path
) -> tuple[Path, Path, Path]:
    executable = _resolved(hermes_executable)
    source = _resolved(checkout)
    home = _resolved(profile_home)
    if home.name != "guest":
        raise ValueError("profile home basename must be 'guest'")
    if not source.is_dir():
        raise ValueError(f"checkout directory does not exist: {source}")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"Hermes executable is missing or not executable: {executable}")
    required_files = (home / "config.yaml", home / ".env", home / "cron" / "jobs.json")
    for path in required_files:
        if not path.is_file():
            raise ValueError(f"required Guest path does not exist: {path}")
    return executable, source, home


def isolated_environment(*, checkout: Path, profile_home: Path, path: str) -> dict[str, str]:
    if not path:
        raise ValueError("PATH must not be empty")
    return {
        "HERMES_HOME": str(profile_home),
        "PYTHONPATH": str(checkout),
        "PATH": path,
    }


def generate_plist(
    *,
    hermes_executable: str | Path,
    checkout: str | Path,
    profile_home: str | Path,
    path: str | None = None,
    script_path: str | Path | None = None,
) -> dict[str, Any]:
    executable, source, home = validate_runtime_paths(
        hermes_executable=hermes_executable, checkout=checkout, profile_home=profile_home
    )
    script = _resolved(script_path or __file__)
    if not script.is_file():
        raise ValueError(f"manager script does not exist: {script}")
    environment = isolated_environment(
        checkout=source, profile_home=home, path=path if path is not None else os.environ.get("PATH", "")
    )
    plist: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            str(script),
            "run-once",
            "--hermes-executable",
            str(executable),
            "--checkout",
            str(source),
            "--profile-home",
            str(home),
            "--path",
            environment["PATH"],
        ],
        "StartInterval": INTERVAL_SECONDS,
        "ProcessType": "Background",
        "WorkingDirectory": str(source),
        "EnvironmentVariables": environment,
    }
    validate_plist(plist)
    return plist


def validate_plist(plist: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plist, dict):
        raise ValueError("plist root must be a dictionary")
    if plist.get("Label") != LABEL:
        raise ValueError(f"plist Label must be {LABEL}")
    if plist.get("StartInterval") != INTERVAL_SECONDS:
        raise ValueError(f"plist StartInterval must be {INTERVAL_SECONDS}")
    if plist.get("ProcessType") != "Background":
        raise ValueError("plist ProcessType must be Background")
    arguments = plist.get("ProgramArguments")
    if not isinstance(arguments, list) or len(arguments) != 11 or arguments[2] != "run-once":
        raise ValueError("plist must execute this manager's run-once subcommand")
    if _resolved(arguments[1]) != _resolved(__file__):
        raise ValueError("plist must execute this manager script")
    expected_options = ("--hermes-executable", "--checkout", "--profile-home", "--path")
    if tuple(arguments[3::2]) != expected_options:
        raise ValueError("plist ProgramArguments has an invalid option layout")
    environment = plist.get("EnvironmentVariables")
    if not isinstance(environment, dict) or set(environment) != {"HERMES_HOME", "PYTHONPATH", "PATH"}:
        raise ValueError("plist environment must contain only HERMES_HOME, PYTHONPATH, and PATH")
    if Path(str(environment["HERMES_HOME"])).name != "guest":
        raise ValueError("plist HERMES_HOME must name the guest profile")
    if str(arguments[8]) != str(environment["HERMES_HOME"]):
        raise ValueError("plist --profile-home and HERMES_HOME must match")
    if str(arguments[6]) != str(environment["PYTHONPATH"]):
        raise ValueError("plist --checkout and PYTHONPATH must match")
    if str(arguments[10]) != str(environment["PATH"]):
        raise ValueError("plist --path and PATH must match")
    if str(environment["PYTHONPATH"]) != str(plist.get("WorkingDirectory")):
        raise ValueError("plist PYTHONPATH and WorkingDirectory must match")
    return plist


def plist_bytes(plist: dict[str, Any]) -> bytes:
    data = plistlib.dumps(validate_plist(plist), fmt=plistlib.FMT_XML, sort_keys=True)
    validate_plist(plistlib.loads(data))
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _launchctl(
    runner: LaunchctlRunner, arguments: list[str]
) -> subprocess.CompletedProcess[str]:
    return runner(arguments, capture_output=True, text=True, check=False, timeout=15)


def install(
    plist: dict[str, Any],
    *,
    launch_agents_dir: str | Path | None = None,
    load: bool = True,
    dry_run: bool = False,
    runner: LaunchctlRunner = subprocess.run,
    uid: int | None = None,
) -> dict[str, Any]:
    data = plist_bytes(plist)
    directory = _resolved(launch_agents_dir or Path.home() / "Library" / "LaunchAgents")
    destination = directory / PLIST_NAME
    changed = not destination.is_file() or destination.read_bytes() != data
    result: dict[str, Any] = {
        "path": str(destination), "changed": changed, "dry_run": dry_run, "loaded": False
    }
    if dry_run:
        return result
    if changed:
        _atomic_write(destination, data)
    if load:
        domain = f"gui/{os.getuid() if uid is None else uid}"
        bootstrap = _launchctl(runner, ["launchctl", "bootstrap", domain, str(destination)])
        if bootstrap.returncode not in (0, 5):
            raise RuntimeError(bootstrap.stderr.strip() or "launchctl bootstrap failed")
        kickstart = _launchctl(runner, ["launchctl", "kickstart", f"{domain}/{LABEL}"])
        if kickstart.returncode != 0:
            raise RuntimeError(kickstart.stderr.strip() or "launchctl kickstart failed")
        result["loaded"] = True
    return result


def uninstall(
    *,
    launch_agents_dir: str | Path | None = None,
    unload: bool = True,
    dry_run: bool = False,
    runner: LaunchctlRunner = subprocess.run,
    uid: int | None = None,
) -> dict[str, Any]:
    directory = _resolved(launch_agents_dir or Path.home() / "Library" / "LaunchAgents")
    destination = directory / PLIST_NAME
    existed = destination.is_file()
    result = {"path": str(destination), "removed": existed and not dry_run, "dry_run": dry_run}
    if dry_run or not existed:
        return result
    if unload:
        domain = f"gui/{os.getuid() if uid is None else uid}"
        _launchctl(runner, ["launchctl", "bootout", f"{domain}/{LABEL}"])
    destination.unlink()
    return result


def _parse_launchctl_print(output: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, pattern in (
        ("pid", r"\bpid\s*=\s*(\d+)"),
        ("last_exit_status", r"\blast exit code\s*=\s*(-?\d+)"),
        ("state", r"\bstate\s*=\s*([^\n]+)"),
    ):
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            parsed[key] = int(value) if key != "state" else value
    return parsed


def status(
    *,
    launch_agents_dir: str | Path | None = None,
    runner: LaunchctlRunner = subprocess.run,
    uid: int | None = None,
) -> dict[str, Any]:
    directory = _resolved(launch_agents_dir or Path.home() / "Library" / "LaunchAgents")
    destination = directory / PLIST_NAME
    result: dict[str, Any] = {
        "path": str(destination), "installed": destination.is_file(), "valid": False, "loaded": False
    }
    if not result["installed"]:
        return result
    try:
        validate_plist(plistlib.loads(destination.read_bytes()))
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        result["error"] = str(exc)
        return result
    result["valid"] = True
    domain = f"gui/{os.getuid() if uid is None else uid}"
    printed = _launchctl(runner, ["launchctl", "print", f"{domain}/{LABEL}"])
    result["loaded"] = printed.returncode == 0
    if result["loaded"]:
        result.update(_parse_launchctl_print(printed.stdout))
    return result


def run_once(
    *,
    hermes_executable: str | Path,
    checkout: str | Path,
    profile_home: str | Path,
    path: str | None = None,
    runner: LaunchctlRunner = subprocess.run,
) -> int:
    executable, source, home = validate_runtime_paths(
        hermes_executable=hermes_executable, checkout=checkout, profile_home=profile_home
    )
    environment = isolated_environment(
        checkout=source, profile_home=home, path=path if path is not None else os.environ.get("PATH", "")
    )
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "cron-guest.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "overlap-skipped", "lock": str(lock_path)}, sort_keys=True))
            return 0
        try:
            completed = runner(
                [str(executable), "--profile", "guest", "cron", "tick"],
                cwd=str(source),
                env=environment,
                check=False,
                timeout=RUN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            print(json.dumps({"status": "timeout", "timeout_seconds": RUN_TIMEOUT_SECONDS}, sort_keys=True))
            return 124
    print(json.dumps({"status": "completed", "returncode": completed.returncode}, sort_keys=True))
    return completed.returncode


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hermes-executable", required=True)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--profile-home", required=True)
    parser.add_argument("--path", help="explicit PATH; defaults to the current PATH")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    subcommands = parser.add_subparsers(dest="command", required=True)
    generate = subcommands.add_parser("generate", help="emit a validated LaunchAgent plist")
    _add_runtime_arguments(generate)
    generate.add_argument("--output", type=Path)
    install_parser = subcommands.add_parser("install", help="atomically install the LaunchAgent")
    _add_runtime_arguments(install_parser)
    install_parser.add_argument("--launch-agents-dir", type=Path)
    install_parser.add_argument("--no-load", action="store_true")
    install_parser.add_argument("--dry-run", action="store_true")
    uninstall_parser = subcommands.add_parser("uninstall", help="idempotently remove the LaunchAgent")
    uninstall_parser.add_argument("--launch-agents-dir", type=Path)
    uninstall_parser.add_argument("--no-unload", action="store_true")
    uninstall_parser.add_argument("--dry-run", action="store_true")
    status_parser = subcommands.add_parser("status", help="inspect the installed plist and launchd state")
    status_parser.add_argument("--launch-agents-dir", type=Path)
    run_parser = subcommands.add_parser("run-once", help="run one bounded Guest cron tick")
    _add_runtime_arguments(run_parser)
    return parser


def _dispatch(args: argparse.Namespace) -> int:
    if args.command in {"generate", "install"}:
        plist = generate_plist(
            hermes_executable=args.hermes_executable,
            checkout=args.checkout,
            profile_home=args.profile_home,
            path=args.path,
        )
        if args.command == "generate":
            data = plist_bytes(plist)
            if args.output:
                _atomic_write(args.output.resolve(), data)
            else:
                sys.stdout.buffer.write(data)
            return 0
        print(json.dumps(install(
            plist,
            launch_agents_dir=args.launch_agents_dir,
            load=not args.no_load,
            dry_run=args.dry_run,
        ), sort_keys=True))
        return 0
    if args.command == "uninstall":
        print(json.dumps(uninstall(
            launch_agents_dir=args.launch_agents_dir,
            unload=not args.no_unload,
            dry_run=args.dry_run,
        ), sort_keys=True))
        return 0
    if args.command == "status":
        print(json.dumps(status(launch_agents_dir=args.launch_agents_dir), sort_keys=True))
        return 0
    return run_once(
        hermes_executable=args.hermes_executable,
        checkout=args.checkout,
        profile_home=args.profile_home,
        path=args.path,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return _dispatch(args)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
