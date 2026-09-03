"""Compatibility seam for the externally versioned restart implementation."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_cli.external_support import load_support_module

def _implementation():
    return load_support_module(
        "hermes_studio_ops.restart_surfaces",
        "support/hermes-studio-ops/src/hermes_studio_ops/restart_surfaces.py",
    )


def enqueue_detached_restart(*args: Any, **kwargs: Any):
    """Delegate the stable gateway/CLI seam to the support implementation."""
    return _implementation().enqueue_detached_restart(*args, **kwargs)


def main(argv: Iterable[str] | None = None) -> int:
    """Run the external implementation under the historical module command."""
    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        return int(_implementation().main(arguments))
    except Exception as exc:
        return _record_bootstrap_failure(arguments, exc)


def _argument_value(arguments: list[str], name: str, default: str = "") -> str:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError):
        return default


def _record_bootstrap_failure(arguments: list[str], exc: Exception) -> int:
    """Preserve the detached completion contract when support cannot load."""
    scope = _argument_value(arguments, "--scope", "hermes")
    marker = _argument_value(arguments, "--completion-marker")
    tty_path = _argument_value(arguments, "--notify-tty")
    log_path = Path.home() / ".hermes" / "logs" / "restart-surfaces.log"
    message = f"Hermes surfaces restart bootstrap failed. Check {log_path}"
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}: {exc}\n")
    except OSError:
        pass
    if tty_path:
        try:
            with open(tty_path, "a", encoding="utf-8", buffering=1) as handle:
                handle.write(f"\n{message}\n")
        except OSError:
            pass
    if marker:
        path = Path(marker).expanduser()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            payload = {
                "status": "complete",
                "scope": scope,
                "exit_code": 1,
                "message": message,
                "completed_at": timestamp,
                "log_path": str(log_path),
            }
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = -1
                    json.dump(payload, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except OSError:
            pass
    print(message, file=sys.stderr)
    return 1


def __getattr__(name: str) -> Any:
    """Preserve imports of implementation symbols during the migration."""
    return getattr(_implementation(), name)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
