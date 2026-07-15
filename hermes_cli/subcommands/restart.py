"""``hermes restart`` — safely restart Hermes-owned runtime surfaces."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _finite_nonnegative(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return value


def _completion_marker() -> Path:
    directory = Path.home() / ".hermes" / "restart-status"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return directory / f"restart-{timestamp}-{os.getpid()}.json"


def _wait_for_completion(marker: Path, *, scope: str, timeout: float) -> int:
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while not marker.is_file():
            if time.monotonic() >= deadline:
                print(
                    f"Timed out waiting for restart status; the detached restart may still continue. "
                    f"Status: {marker}"
                )
                return 124
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\nStopped waiting; the detached restart continues. Status: {marker}")
        return 130

    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("completion marker is not an object")
        if payload.get("status") != "complete" or payload.get("scope") != scope:
            raise ValueError("completion marker identity mismatch")
        exit_code = int(payload["exit_code"])
    except (KeyError, OSError, TypeError, ValueError):
        print(f"Restart finished; status file: {marker}")
        return 1
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        print(message.strip())
    else:
        print(f"Restart finished; status file: {marker}")
    return exit_code


def cmd_restart(args: argparse.Namespace) -> None:
    """Queue a detached, drain-aware restart and optionally wait for completion."""
    from hermes_cli.restart_surfaces import enqueue_detached_restart

    if sys.platform != "darwin":
        raise SystemExit("hermes restart currently supports macOS launchd services only")

    scope = "gateways"
    if args.dry_run:
        print(enqueue_detached_restart(scope, dry_run=True))
        return

    marker = _completion_marker()
    print(enqueue_detached_restart(
        scope,
        delay=args.delay,
        completion_marker=str(marker),
        safe_wait_timeout=args.safe_wait_timeout,
    ))
    print(f"Completion status: {marker}")
    if args.wait:
        from hermes_cli.restart_surfaces import DEFAULT_SAFE_WAIT_TIMEOUT

        drain_timeout = args.safe_wait_timeout
        if drain_timeout is None:
            drain_timeout = DEFAULT_SAFE_WAIT_TIMEOUT
        wait_timeout = args.wait_timeout
        if wait_timeout is None:
            # The worker can spend one drain budget before the loop and another
            # at the final WebUI boundary, plus bounded sequential gateway
            # replacement and health checks.
            wait_timeout = (2.0 * drain_timeout) + 6180.0
        exit_code = _wait_for_completion(marker, scope=scope, timeout=wait_timeout)
        if exit_code:
            raise SystemExit(exit_code)


def build_restart_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "restart",
        help="Safely restart Hermes gateways and WebUI/dashboard surfaces",
        description=(
            "Queue a detached, drain-aware restart of Hermes gateways and WebUI/dashboard surfaces. "
            "Gateways enter their native drain mode; WebUI is restarted only after an idle "
            "health probe immediately before restart. Readiness is checked afterward. Background workers "
            "without a drain protocol are intentionally excluded."
        ),
    )
    parser.add_argument(
        "--dry-run",
        "--plan",
        action="store_true",
        help="Print the exact restart plan without changing runtime state",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the detached worker's completion status (Ctrl-C stops waiting only)",
    )
    parser.add_argument(
        "--delay",
        type=_finite_nonnegative,
        default=1.0,
        help="Seconds before the detached worker starts (default: 1)",
    )
    parser.add_argument(
        "--safe-wait-timeout",
        type=_finite_nonnegative,
        default=None,
        help="Maximum seconds to wait for active work to drain (default: helper policy, currently 24h)",
    )
    parser.add_argument(
        "--wait-timeout",
        type=_finite_nonnegative,
        default=None,
        help="Maximum seconds for --wait (default: derived from all drain/replacement budgets)",
    )
    parser.set_defaults(func=cmd_restart)
