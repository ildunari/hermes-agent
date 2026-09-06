"""External drain-control marker contract (dashboard → gateway).

Versioned transactions survive restarts/expiry and require native owner release.
The legacy dashboard marker below retains its epoch and one-hour expiry behavior.

No control channel exists into a running gateway, so begin/cancel-drain writes
(or removes) ``{HERMES_HOME}/.drain_request.json`` and a gateway watcher reacts;
an ACTIVE marker means ``gateway_state -> "draining"``.  Two lenient staleness
signals (either suffices): epoch mismatch (HERMES_HOME is a durable volume on
Hermes Cloud, so a marker survives the restart a drain-gated action ends in and
would park the fresh gateway in ``draining`` forever) and expiry (same-epoch
orphan past :data:`DRAIN_REQUEST_MAX_AGE_SECONDS`; re-writing refreshes it).
Reading never raises: a malformed file reads as ``{}`` — still drain-active
(fail-safe toward quiescing).  Staleness rejects only on a *definite* verdict.
"""
from __future__ import annotations

import contextlib
import functools
import json
import logging
import asyncio
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from gateway.memory_status import _parse_iso
from hermes_constants import get_hermes_home
from utils import atomic_json_write
from hermes_cli.active_sessions import _FileLock

_log = logging.getLogger(__name__)

_DRAIN_REQUEST_FILENAME = ".drain_request.json"
# Drain-gated lifecycle actions complete in minutes; an hour bounds the wedge a leaked
# marker can cause. Long drains refresh the marker instead of raising this.
# Max-age fallback for a same-epoch orphaned marker (#85433). Long-running drains refresh the marker via
# write_drain_request() (idempotent re-write bumps ``requested_at``) rather than raising this bound.
DRAIN_REQUEST_MAX_AGE_SECONDS = 3600.0
# Dedup for the expired-marker warning (the watcher re-reads every second); keyed by
# ``requested_at`` so a keep-alive re-write that later expires logs again.
_expiry_logged_for: Optional[str] = None


@functools.lru_cache(maxsize=1)
def current_instantiation_epoch() -> str:
    """Identity of THIS container / VM instantiation ("<boot_id>:<pid1_start>").

    Stable for the life of PID 1 (a gateway-only respawn keeps honouring an
    in-flight drain) but changes when the machine is recreated: boot_id on a VM
    reboot, PID 1's start time on ``docker restart``.  ``""`` when neither is
    readable (non-Linux, no ``/proc``) disables the epoch check — never fail-closed.
    """
    boot_id = pid1_start = ""
    with contextlib.suppress(OSError):
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    with contextlib.suppress(OSError, IndexError):
        # "<pid> (<comm>) <state> ...": comm may contain spaces/parens, so split on the
        # LAST ')'. starttime is field 22 (1-indexed) = tail index 19.
        pid1_start = Path("/proc/1/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19]
    return f"{boot_id}:{pid1_start}" if (boot_id or pid1_start) else ""


def drain_request_path(home: Optional[Path] = None) -> Path:
    """Absolute path to the drain-request marker, respecting HERMES_HOME."""
    return Path(home if home is not None else get_hermes_home()) / _DRAIN_REQUEST_FILENAME


def write_drain_request(
    *, principal: str = "drain-control", suppress_notification: bool = False, home: Optional[Path] = None
) -> dict[str, Any]:
    """Write the begin-drain marker atomically; returns the payload.

    Re-writing refreshes ``requested_at`` (keep-alive past the max-age).
    ``suppress_notification`` skips ONLY the home-channel "gateway shutting down"
    broadcast (the per-session interrupt ping is never suppressed); which drains
    are quiet is the caller's policy.  Stamped with the instantiation epoch so a
    copy surviving a machine restart on the durable volume reads as stale.
    """
    payload = {
        "action": "drain", "requested_at": datetime.now(timezone.utc).isoformat(), "principal": principal,
        "epoch": current_instantiation_epoch(), "suppress_notification": bool(suppress_notification),
    }
    path = drain_request_path(home)
    with _FileLock(path.with_suffix(".lock")):
        existing = read_drain_request(home=home)
        if existing and "protocol_version" in existing:
            raise ValueError("transactional drain requires its matching owner/token")
        atomic_json_write(path, payload)
    return payload


def clear_drain_request(*, home: Optional[Path] = None) -> bool:
    """Remove the drain marker (cancel-drain, idempotent). Returns True if one existed."""
    path = drain_request_path(home)
    with _FileLock(path.with_suffix(".lock")):
        existing = read_drain_request(home=home)
        if existing and "protocol_version" in existing:
            raise ValueError("transactional drain requires its matching owner/token")
        try:
            path.unlink()
            return True
        except OSError as e:
            if not isinstance(e, FileNotFoundError):
                _log.warning("drain-control: failed to remove %s: %s", path, e)
            return False


def _marker_is_expired(body: dict[str, Any]) -> bool:
    """True iff ``requested_at`` parses AND is older than the max-age.

    Missing/unparseable and future-dated (clock skew) timestamps are honoured.
    Logged once per marker, not per poll — the operator's breadcrumb for a leak.

    See #85433.
    """
    global _expiry_logged_for
    raw = body.get("requested_at")
    requested_at = _parse_iso(raw)
    if requested_at is None:
        return False
    age = (datetime.now(timezone.utc) - requested_at).total_seconds()
    if age <= DRAIN_REQUEST_MAX_AGE_SECONDS:
        return False
    if _expiry_logged_for != raw:
        _expiry_logged_for = raw
        _log.warning(
            "drain-control: ignoring expired drain marker (requested_at=%s, age=%.0fs > max %.0fs, principal=%s) "
            "— the drain that wrote it was never cancelled; treating as stale so the gateway keeps accepting turns.",
            raw, age, DRAIN_REQUEST_MAX_AGE_SECONDS, body.get("principal"),
        )
    return True


def _active_drain_body(home: Optional[Path]) -> Optional[dict[str, Any]]:
    """Marker body if present AND not stale (definite epoch mismatch or expired), else None."""
    body = read_drain_request(home=home)
    if body is None:
        return None
    if "protocol_version" in body:
        return body  # Transactions have no time/boot-based release authority.
    current, marker_epoch = current_instantiation_epoch(), body.get("epoch")
    if (current and marker_epoch and marker_epoch != current) or _marker_is_expired(body):
        return None
    return body


def drain_requested(*, home: Optional[Path] = None) -> bool:
    """True iff an active (present, same-epoch, unexpired) begin-drain marker exists.

    A marker whose ``epoch`` does not match the current instantiation epoch is treated as absent: it
    survived a container/VM restart (HERMES_HOME is a durable Fly volume on Hermes Cloud) and the lifecycle
    action that triggered the drain has already completed — honouring it would wedge the freshly-restarted
    gateway in ``draining`` (NS-570). A marker whose ``requested_at`` is older than
    :data:`DRAIN_REQUEST_MAX_AGE_SECONDS` is likewise treated as absent: it is a same-epoch orphan whose
    drain-gated action completed without a restart and was never cancelled (#85433). Both staleness checks
    are lenient (see :func:`_marker_epoch_is_stale` / :func:`_marker_is_expired`): a legacy/corrupt marker
    with no epoch and no timestamp, or an environment without ``/proc``, still reads as drain-active.
    """
    return _active_drain_body(home) is not None


def drain_notification_suppressed(*, home: Optional[Path] = None) -> bool:
    """True iff an ACTIVE marker asks to suppress the shutdown broadcast.

    Same activeness rule as :func:`drain_requested`, so an orphan can never silence
    a fresh gateway; a marker without the field reads False (fail toward louder).

    "Active" means exactly what :func:`drain_requested` means — a marker present AND stamped with the
    current instantiation epoch AND not past its max-age. A stale (other-epoch) marker that survived a
    machine restart on the durable HERMES_HOME volume, or an expired same-epoch orphan (#85433), is ignored
    here just as it is for drain state (NS-570): we must never let an orphaned marker's flag silence a
    *fresh* gateway's legitimate shutdown broadcast.
    """
    body = _active_drain_body(home)
    return bool(body and body.get("suppress_notification"))


def read_drain_request(*, home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return the marker payload, ``{}`` if present but unparseable, ``None`` if absent. Never raises."""
    path = drain_request_path(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        if not isinstance(e, FileNotFoundError):
            _log.warning("drain-control: failed to read %s: %s", path, e)
            return {}
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


class GatewayMaintenance:
    """Native gateway transaction fence; sessions remain in their existing owners.

    Release opens this generation only. The disk bootstrap remains closed until
    the coordinator has verified and released the replacement generation.
    """

    def __init__(self, home):
        self.home = Path(home)
        self.path = drain_request_path(self.home)
        self.generation = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.pending_admissions = 0
        self.waiting_turns = 0
        self.released_request_token = None
        self.request_token = self._token()
        self.provider_supported = True

    def _token(self):
        body = read_drain_request(home=self.home)
        if body is None or (body.get("action") == "drain" and "protocol_version" not in body):
            return None
        token = body.get("request_token")
        if body.get("protocol_version") != 1 or not isinstance(token, str) or not token:
            raise ValueError("invalid gateway maintenance bootstrap")
        return token

    @property
    def closed(self):
        return self.request_token is not None

    def _validate(self, generation, token):
        if generation != self.generation:
            raise ValueError("stale gateway owner generation")
        if not isinstance(token, str) or not token or len(token) > 256:
            raise ValueError("request_token required (1..256 characters)")

    def begin(self, generation, token):
        with self.lock, _FileLock(self.path.with_suffix(".lock")):
            self._validate(generation, token)
            if not self.provider_supported:
                raise ValueError("cron provider lacks native maintenance admission support")
            if self.released_request_token == token:
                raise ValueError("released transaction cannot be begun again")
            if self._token() not in (None, token) or self.request_token not in (None, token):
                raise ValueError("gateway held by another transaction")
            atomic_json_write(self.path, {
                "action": "drain", "protocol_version": 1, "request_token": token,
                "suppress_notification": True,
            })
            self.request_token = token
            self.released_request_token = None

    def release(self, generation, token):
        with self.lock:
            self._validate(generation, token)
            if not self.provider_supported:
                raise ValueError("cron provider lacks native maintenance admission support")
            held = self._token()
            if held != token and not (held is None and self.released_request_token == token and not self.closed):
                raise ValueError("bootstrap token does not match release")
            if self.request_token not in (None, token):
                raise ValueError("gateway held by another transaction")
            self.request_token = None
            self.released_request_token = token

    def finalize(self, generation, token):
        with self.lock, _FileLock(self.path.with_suffix(".lock")):
            self._validate(generation, token)
            if self.closed or self.released_request_token != token or self._token() not in (None, token):
                raise ValueError("gateway must acknowledge release before bootstrap finalization")
            self.path.unlink(missing_ok=True)

    @contextlib.contextmanager
    def admission(self):
        # Count the pre-registration gap, rather than holding a lock while cron
        # calls back onto the gateway loop (which would deadlock status/begin).
        with self.lock:
            admitted = not self.closed
            if admitted:
                self.pending_admissions += 1
        try:
            yield admitted
        finally:
            if admitted:
                with self.lock:
                    self.pending_admissions -= 1

    async def wait_until_open(self, runner, *, queued=False):
        if queued:
            self.waiting_turns += 1
        try:
            while self.closed:
                if runner._shutdown_event.is_set():
                    return False
                await asyncio.sleep(0.05)
            return not runner._shutdown_event.is_set()
        finally:
            if queued:
                self.waiting_turns -= 1

    def status(self, runner):
        with self.lock:
            if not self.provider_supported:
                raise ValueError("cron provider lacks native maintenance admission support")
            delegates = sys.modules.get("tools.delegate_tool_registry")
            asynchronous = sys.modules.get("tools.async_delegation")
            from gateway.config import Platform
            api = runner.adapters.get(Platform.API_SERVER)
            api_active = api.active_agent_work_count() if api is not None else 0
            active = (runner._running_agent_count() + runner._running_cron_job_count()
                      + api_active + runner._active_deferred_agent_worker_count()
                      + self.pending_admissions)
            queued = sum(len(q) for q in getattr(runner, "_queued_events", {}).values())
            queued += len(getattr(runner, "_startup_restore_queue", []))
            queued += self.waiting_turns
            adapters = {id(a): a for a in runner.adapters.values()}
            for group in getattr(runner, "_profile_adapters", {}).values():
                adapters.update({id(a): a for a in group.values()})
            queued += sum(len(getattr(a, "_pending_messages", {})) for a in adapters.values())
            approvals = len(getattr(runner, "_pending_approvals", {}))
            approval = sys.modules.get("tools.approval")
            if approval is not None:
                keys = set(getattr(runner, "_session_sources", {})) | set(runner._running_agents)
                approvals = max(approvals, sum(len(approval.list_gateway_approvals(k)) for k in keys))
            return {
                "owner_pid": os.getpid(), "owner_generation": self.generation,
                "owner_kind": "gateway", "hermes_home": str(self.home),
                "request_token": self.request_token, "admissions_closed": self.closed,
                "bootstrap_held": self._token() is not None,
                "released_request_token": self.released_request_token,
                "active_turns": active, "queued_turns": queued,
                "delegation_count": max(len(delegates.list_active_subagents()) if delegates else 0,
                                        asynchronous.active_count() if asynchronous else 0),
                "pending_approvals": approvals,
                "maintenance_supported": self.provider_supported,
            }


_gateway_owner = None


def gateway_maintenance(runner):
    global _gateway_owner
    owner = getattr(runner, "_maintenance_owner", None)
    if owner is None:
        owner = GatewayMaintenance(get_hermes_home())
        from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler, scheduler_for_profile_mode
        provider = scheduler_for_profile_mode(resolve_cron_scheduler(),
            multiplex_profiles=bool(getattr(runner.config, "multiplex_profiles", False)))
        owner.provider_supported = type(provider) is InProcessCronScheduler
        runner._maintenance_owner = owner
        _gateway_owner = owner
    return owner


def gateway_maintenance_control(runner, body):
    owner = gateway_maintenance(runner)
    action = body.get("action", "status")
    generation, token = body.get("owner_generation"), body.get("request_token")
    if generation is not None and generation != owner.generation:
        raise ValueError("stale gateway owner generation")
    if action == "begin":
        owner.begin(generation, token)
        runner._enter_external_drain()
    elif action == "release":
        if body.get("finalize_bootstrap") and body.get("expected_owner_generations") != [owner.generation]:
            raise ValueError("replacement gateway inventory changed")
        owner.release(generation, token)
        runner._exit_external_drain()
        if body.get("finalize_bootstrap"):
            owner.finalize(generation, token)
    elif action != "status":
        raise ValueError("unknown maintenance action")
    return {"protocol_version": 1, "owners": [owner.status(runner)]}


@contextlib.contextmanager
def gateway_cron_admission():
    """Cover the native tick's check-to-registration interval when hosted here."""
    if _gateway_owner is None:
        yield True
    else:
        with _gateway_owner.admission() as admitted:
            yield admitted
