"""Active-work persistence and runtime identity helpers.

This module deliberately has no gateway runner or plugin imports at module
load time.  The helpers below are used by adapter, cron, status, and watchdog
paths that must remain safe before plugin discovery is available.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)

# Keep the persisted restart-drain count synchronized with work claimed by
# API and cron paths outside GatewayRunner's normal turn boundaries.
_ACTIVE_AGENTS_PERSIST_MIN_INTERVAL = 0.25
_active_agents_persist_state: Dict[str, float] = {"ts": 0.0, "count": -1.0}
_active_agents_persist_lock = threading.Lock()


def persist_active_agents_now() -> None:
    """Best-effort, throttled persist of the live active-work count."""
    from gateway.run import _gateway_runner_ref

    runner = _gateway_runner_ref()
    if runner is None:
        return
    try:
        with _active_agents_persist_lock:
            count = runner._active_work_count()
            now = time.monotonic()
            state = _active_agents_persist_state
            crossing_zero = (count == 0) != (state["count"] == 0)
            if (
                not crossing_zero
                and (now - state["ts"]) < _ACTIVE_AGENTS_PERSIST_MIN_INTERVAL
            ):
                return
            state["ts"] = now
            state["count"] = float(count)
        runner._persist_active_agents()
    except Exception:
        # Drain telemetry must never break the work path it observes.
        pass


def persist_active_agents(runner: Any) -> None:
    """Persist a runner's live in-flight agent count."""
    try:
        from gateway.status import write_runtime_status

        write_runtime_status(active_agents=runner._active_work_count)
    except Exception:
        pass


def process_owns_runtime_status(existing: Optional[dict[str, Any]]) -> bool:
    """Return whether this process may stamp the live gateway's identity."""
    from gateway import status as _status

    if _status._gateway_lock_handle is not None:
        return True
    me = _status.os.getpid()
    pid_record = _status._read_pid_record()
    file_pid = _status._pid_from_record(pid_record)
    if file_pid == me:
        return True
    if file_pid is not None and _status.runtime_status_pid_is_live(pid_record):
        return False
    existing_pid = _status._pid_from_record(existing)
    if existing_pid is None or existing_pid == me:
        return True
    return not _status.runtime_status_pid_is_live(existing)


def refresh_runtime_status_identity() -> None:
    """Best-effort owner-path identity restamp for the loop heartbeat."""
    try:
        from gateway.status import write_runtime_status

        write_runtime_status()
    except Exception:
        logger.debug("Failed to refresh gateway runtime status identity", exc_info=True)
