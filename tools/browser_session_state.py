"""Reload-stable process state shared by browser tool module incarnations."""

from __future__ import annotations

import atexit
import threading
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any


ACTIVE_SESSIONS: dict[str, dict[str, Any]] = {}
RECORDING_SESSIONS: set[str] = set()
LAST_ACTIVE_SESSION_KEY: dict[str, str] = {}
IN_APP_SESSION_EXPECTATIONS: dict[str, float] = {}
SESSION_LAST_ACTIVITY: dict[str, float] = {}

CLEANUP_LOCK = threading.Lock()
IN_APP_SESSION_CONDITION = threading.Condition(CLEANUP_LOCK)


@dataclass
class CleanupRuntime:
    """Mutable cleanup ownership that survives ``browser_tool`` reloads."""

    thread: threading.Thread | None = None
    running: bool = False
    done: bool = False
    emergency_callback: Callable[[], None] | None = None
    stop_callback: Callable[[], None] | None = None
    atexit_registered: bool = False


CLEANUP_RUNTIME = CleanupRuntime()


def _run_exit_cleanup() -> None:
    """Dispatch exit cleanup to the newest loaded browser-tool incarnation."""

    emergency = CLEANUP_RUNTIME.emergency_callback
    stop = CLEANUP_RUNTIME.stop_callback
    if emergency is not None:
        emergency()
    if stop is not None:
        stop()


def register_exit_cleanup(
    emergency_callback: Callable[[], None], stop_callback: Callable[[], None]
) -> None:
    """Install one process-wide atexit handle while refreshing its callbacks."""

    CLEANUP_RUNTIME.emergency_callback = emergency_callback
    CLEANUP_RUNTIME.stop_callback = stop_callback
    if not CLEANUP_RUNTIME.atexit_registered:
        atexit.register(_run_exit_cleanup)
        CLEANUP_RUNTIME.atexit_registered = True
