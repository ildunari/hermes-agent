"""Persistent proactive scheduler for contact-isolated gateway profiles.

The scheduler owns *when*. Interest shares and check-ins pass through isolated
fetch/gate/compose callbacks. Live payloads are durably prepared here and sent
only by the separately gated exactly-once transport edge.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import inspect
import json
import logging
import math
from pathlib import Path
import random
import re
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid
from enum import Enum

from gateway.contact_memory.schema import (
    GateDecision,
    Interest,
    InterestState,
    InterestValence,
    ProactiveOutcome,
    ProactiveSend,
    ProactiveSendKind,
)
from gateway.contact_memory.store import ContactMemoryStore, opaque_contact_filename
from gateway.proactive_checkin import (
    CheckinInitiationResult,
    plan_checkin,
    push_into_active_hours,
)

_WEEK = 7 * 86_400.0
_DAY = 86_400.0
_DIRECT_TYPES = frozenset({"dm", "direct", "private"})
_NEGATIVE_OUTCOMES = frozenset({"ignored", "dismissed"})
_OUTCOME_VALENCE = {
    "engaged": "positive",
    "acknowledged": "neutral",
    "ignored": "neutral",
    "dismissed": "negative",
}
_OUTCOME_TEXT_LIMIT = 4000
logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_contact (
  contact_hash TEXT PRIMARY KEY,
  contact_id TEXT,
  route_json TEXT,
  profile_name TEXT NOT NULL,
  timezone TEXT NOT NULL,
  principal TEXT NOT NULL CHECK(principal IN ('owner','guest')),
  inbound_version INTEGER NOT NULL DEFAULT 0,
  last_inbound_at REAL,
  serious_until REAL,
  pending_checkin_kind TEXT CHECK(pending_checkin_kind IN ('serious','open_loop')),
  pending_checkin_reason TEXT,
  route_fingerprint TEXT,
  disabled_until REAL,
  negative_streak INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_inbound (
  contact_hash TEXT NOT NULL REFERENCES proactive_contact(contact_hash) ON DELETE CASCADE,
  message_id TEXT NOT NULL,
  received_at REAL NOT NULL,
  PRIMARY KEY(contact_hash, message_id)
);
CREATE INDEX IF NOT EXISTS proactive_inbound_recent
  ON proactive_inbound(contact_hash, received_at DESC);
CREATE TABLE IF NOT EXISTS proactive_ingress_observed (
  message_id TEXT PRIMARY KEY,
  observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_slot (
  slot_id TEXT PRIMARY KEY,
  contact_hash TEXT NOT NULL REFERENCES proactive_contact(contact_hash) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('interest_share','checkin','exploration')),
  interest_id TEXT,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('armed','claimed','fired','cancelled','suppressed')),
  fire_at REAL NOT NULL,
  inbound_version INTEGER NOT NULL,
  ingress_sequence INTEGER NOT NULL DEFAULT 0,
  claim_token TEXT,
  claim_until REAL,
  reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  CHECK((status='claimed' AND claim_token IS NOT NULL AND claim_until IS NOT NULL)
     OR (status!='claimed'))
);
CREATE INDEX IF NOT EXISTS proactive_slot_due ON proactive_slot(status, fire_at);
CREATE TABLE IF NOT EXISTS proactive_action (
  action_id TEXT PRIMARY KEY,
  slot_id TEXT UNIQUE,
  contact_hash TEXT NOT NULL REFERENCES proactive_contact(contact_hash) ON DELETE CASCADE,
  interest_id TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('interest_share','checkin','exploration')),
  status TEXT NOT NULL CHECK(status IN ('sent','dry_run','suppressed')),
  sent_at REAL,
  outcome TEXT CHECK(outcome IN ('engaged','acknowledged','ignored','dismissed')),
  outcome_at REAL,
  outcome_status TEXT CHECK(outcome_status IN ('provisional','confirmed')),
  outcome_source_id TEXT,
  inbound_version INTEGER NOT NULL,
  reason TEXT NOT NULL,
  created_at REAL NOT NULL,
  CHECK((status='sent' AND sent_at IS NOT NULL) OR (status!='sent' AND sent_at IS NULL)),
  CHECK((outcome IS NULL AND outcome_at IS NULL AND outcome_status IS NULL) OR
        (outcome IS NOT NULL AND outcome_at IS NOT NULL AND outcome_status IS NOT NULL
         AND sent_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS proactive_action_recent
  ON proactive_action(contact_hash, sent_at DESC, created_at DESC);
CREATE TABLE IF NOT EXISTS proactive_outcome_confirmation (
  action_id TEXT PRIMARY KEY REFERENCES proactive_action(action_id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('claimed','confirmed','failed')),
  claimed_at REAL NOT NULL,
  provisional_at REAL NOT NULL,
  completed_at REAL,
  provisional_outcome TEXT NOT NULL CHECK(provisional_outcome IN ('engaged','acknowledged','ignored','dismissed')),
  confirmed_outcome TEXT CHECK(confirmed_outcome IN ('engaged','acknowledged','ignored','dismissed')),
  confirmed_valence TEXT CHECK(confirmed_valence IN ('positive','neutral','negative')),
  input_sha256 TEXT NOT NULL,
  input_chars INTEGER NOT NULL CHECK(input_chars BETWEEN 0 AND 4000),
  source_id TEXT NOT NULL,
  error_code TEXT,
  projection_state TEXT NOT NULL DEFAULT 'not_required'
    CHECK(projection_state IN ('not_required','pending','complete','failed')),
  projection_error TEXT,
  CHECK((status='claimed' AND completed_at IS NULL AND confirmed_outcome IS NULL
         AND confirmed_valence IS NULL AND error_code IS NULL)
     OR (status='confirmed' AND completed_at IS NOT NULL AND confirmed_outcome IS NOT NULL
         AND confirmed_valence IS NOT NULL AND error_code IS NULL)
     OR (status='failed' AND completed_at IS NOT NULL AND confirmed_outcome IS NULL
         AND confirmed_valence IS NULL AND error_code IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS proactive_cancelled_fetch (
  source_slot_id TEXT PRIMARY KEY,
  contact_hash TEXT NOT NULL REFERENCES proactive_contact(contact_hash) ON DELETE CASCADE,
  topic_hash TEXT NOT NULL,
  candidate_json TEXT NOT NULL,
  cached_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  consumed_at REAL
);
CREATE INDEX IF NOT EXISTS proactive_cancelled_fetch_reuse
  ON proactive_cancelled_fetch(contact_hash,topic_hash,expires_at,consumed_at);
CREATE TABLE IF NOT EXISTS proactive_delivery (
  slot_id TEXT PRIMARY KEY REFERENCES proactive_slot(slot_id),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  not_before REAL,
  state TEXT NOT NULL CHECK(state IN ('prepared','sending','retry_wait','sent','suppressed','delivery_unknown','partial_delivery','failed')),
  payload_hash TEXT NOT NULL,
  prepared_payload TEXT,
  prepared_image_url TEXT,
  kill_generation INTEGER NOT NULL DEFAULT 0,
  transport_message_id TEXT,
  last_error_class TEXT,
  projection_state TEXT NOT NULL DEFAULT 'not_required' CHECK(projection_state IN ('not_required','pending','complete','failed')),
  projection_error TEXT,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_health (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""

_OWNERSHIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_contact_owner (
  contact_hash TEXT PRIMARY KEY,
  profile_name TEXT NOT NULL,
  state_db TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_transport_owner (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  runner_id TEXT NOT NULL,
  adapter_id TEXT NOT NULL,
  lease_until REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_global_send (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  slot_id TEXT,
  reserved_until REAL,
  last_visible_at REAL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_global_circuit (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  state TEXT NOT NULL CHECK(state IN ('closed','open')),
  reason TEXT NOT NULL,
  generation INTEGER NOT NULL,
  updated_at REAL NOT NULL
);
"""


def _initialize_schema(con: sqlite3.Connection) -> None:
    """Install the additive schema and repair pre-index duplicate live slots."""
    con.executescript(_SCHEMA)
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(proactive_delivery)")}
    if "prepared_payload" not in columns:
        con.execute("ALTER TABLE proactive_delivery ADD COLUMN prepared_payload TEXT")
    if "prepared_image_url" not in columns:
        con.execute("ALTER TABLE proactive_delivery ADD COLUMN prepared_image_url TEXT")
    if "kill_generation" not in columns:
        con.execute("ALTER TABLE proactive_delivery ADD COLUMN kill_generation INTEGER NOT NULL DEFAULT 0")
    if "projection_state" not in columns:
        con.execute("ALTER TABLE proactive_delivery ADD COLUMN projection_state TEXT NOT NULL DEFAULT 'not_required'")
    if "projection_error" not in columns:
        con.execute("ALTER TABLE proactive_delivery ADD COLUMN projection_error TEXT")
    contact_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(proactive_contact)")}
    if "route_fingerprint" not in contact_columns:
        con.execute("ALTER TABLE proactive_contact ADD COLUMN route_fingerprint TEXT")
    slot_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(proactive_slot)")}
    if "ingress_sequence" not in slot_columns:
        con.execute("ALTER TABLE proactive_slot ADD COLUMN ingress_sequence INTEGER NOT NULL DEFAULT 0")
    action_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(proactive_action)")}
    if "outcome_status" not in action_columns:
        con.execute("ALTER TABLE proactive_action ADD COLUMN outcome_status TEXT")
    con.execute(
        "UPDATE proactive_action SET outcome_status='confirmed' "
        "WHERE outcome IS NOT NULL AND outcome_status IS NULL"
    )
    if "outcome_source_id" not in action_columns:
        con.execute("ALTER TABLE proactive_action ADD COLUMN outcome_source_id TEXT")
    confirmation_columns = {
        str(row[1])
        for row in con.execute("PRAGMA table_info(proactive_outcome_confirmation)")
    }
    if "projection_state" not in confirmation_columns:
        con.execute(
            "ALTER TABLE proactive_outcome_confirmation ADD COLUMN projection_state TEXT "
            "NOT NULL DEFAULT 'not_required'"
        )
    if "projection_error" not in confirmation_columns:
        con.execute(
            "ALTER TABLE proactive_outcome_confirmation ADD COLUMN projection_error TEXT"
        )
    con.execute("BEGIN IMMEDIATE")
    try:
        duplicates = con.execute(
            """SELECT contact_hash FROM proactive_slot
               WHERE status IN ('armed','claimed') GROUP BY contact_hash HAVING count(*)>1"""
        ).fetchall()
        for duplicate in duplicates:
            rows = con.execute(
                """SELECT slot_id FROM proactive_slot WHERE contact_hash=?
                   AND status IN ('armed','claimed') ORDER BY created_at,slot_id""",
                (duplicate[0],),
            ).fetchall()
            for row in rows[1:]:
                con.execute(
                    """UPDATE proactive_slot SET status='cancelled',reason='duplicate_repaired',
                       claim_token=NULL,claim_until=NULL WHERE slot_id=?""",
                    (row[0],),
                )
        con.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS proactive_one_live_slot
               ON proactive_slot(contact_hash) WHERE status IN ('armed','claimed')"""
        )
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _default_ownership_registry(profile_home: Path) -> Path:
    parent = profile_home.parent
    root = parent.parent if parent.name == "profiles" else parent
    # Production profile homes share the Hermes-root registry. Standalone
    # roots (tests/embedders) keep ownership local instead of leaking through
    # their common parent.
    return root / "proactive-contact-ownership.db" if parent.name == "profiles" else profile_home / "proactive-contact-ownership.db"


class ProactiveOwnershipRegistry:
    """Cross-profile owner registry serialized by SQLite's process-safe write lock."""

    def __init__(self, path: str | Path, *, timeout: float = 10.0) -> None:
        self.path = Path(path).expanduser().resolve()
        self.timeout = timeout
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as con:
            con.executescript(_OWNERSHIP_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        return con

    def claim(self, contact_hash: str, profile_name: str, state_db: Path, *, now: float) -> None:
        con = self._connect()
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute(
                "SELECT profile_name,state_db FROM proactive_contact_owner WHERE contact_hash=?",
                (contact_hash,),
            ).fetchone()
            if row is not None and row["profile_name"] != profile_name:
                con.execute(
                    "INSERT INTO proactive_global_circuit VALUES(1,'open','ownership_conflict',1,?) ON CONFLICT(singleton) DO UPDATE SET state='open',reason='ownership_conflict',generation=proactive_global_circuit.generation+1,updated_at=excluded.updated_at",
                    (now,),
                )
                con.execute("COMMIT")
                raise RuntimeError(
                    f"contact ownership conflict across profiles: {row['profile_name']} vs {profile_name}"
                )
            con.execute(
                """INSERT INTO proactive_contact_owner(contact_hash,profile_name,state_db,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(contact_hash) DO UPDATE SET
                   state_db=excluded.state_db,updated_at=excluded.updated_at""",
                (contact_hash, profile_name, str(state_db), now),
            )
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def acquire_transport(self, runner_id: str, adapter_id: str, *, now: float, lease_seconds: int = 120) -> None:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM proactive_transport_owner WHERE singleton=1").fetchone()
            if row is not None and float(row["lease_until"]) > now and (
                row["runner_id"] != runner_id or row["adapter_id"] != adapter_id
            ):
                con.execute(
                    "INSERT INTO proactive_global_circuit VALUES(1,'open','transport_owner_conflict',1,?) ON CONFLICT(singleton) DO UPDATE SET state='open',reason='transport_owner_conflict',generation=proactive_global_circuit.generation+1,updated_at=excluded.updated_at",
                    (now,),
                )
                con.execute("COMMIT")
                raise RuntimeError("proactive transport owner lease conflict")
            con.execute(
                "INSERT INTO proactive_transport_owner VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET runner_id=excluded.runner_id,adapter_id=excluded.adapter_id,lease_until=excluded.lease_until,updated_at=excluded.updated_at",
                (runner_id, adapter_id, now + lease_seconds, now),
            )
            con.execute("COMMIT")

    def validate_transport(self, runner_id: str, adapter_id: str, *, now: float) -> bool:
        with self._connect() as con:
            row = con.execute("SELECT * FROM proactive_transport_owner WHERE singleton=1").fetchone()
        return bool(row and row["runner_id"] == runner_id and row["adapter_id"] == adapter_id and float(row["lease_until"]) >= now)

    def reserve_global_send(self, slot_id: str, *, now: float) -> bool:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            circuit = con.execute("SELECT state FROM proactive_global_circuit WHERE singleton=1").fetchone()
            if circuit is not None and circuit["state"] == "open":
                con.execute("ROLLBACK")
                return False
            row = con.execute("SELECT * FROM proactive_global_send WHERE singleton=1").fetchone()
            if row is not None and (
                (row["reserved_until"] is not None and float(row["reserved_until"]) > now)
                or (row["last_visible_at"] is not None and now - float(row["last_visible_at"]) < 300)
            ):
                con.execute("ROLLBACK")
                return False
            con.execute(
                "INSERT INTO proactive_global_send VALUES(1,?,?,NULL,?) ON CONFLICT(singleton) DO UPDATE SET slot_id=excluded.slot_id,reserved_until=excluded.reserved_until,updated_at=excluded.updated_at",
                (slot_id, now + 120, now),
            )
            con.execute("COMMIT")
            return True

    def finish_global_send(self, slot_id: str, *, sent: bool, now: float) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE proactive_global_send SET reserved_until=NULL,last_visible_at=CASE WHEN ? THEN ? ELSE last_visible_at END,updated_at=? WHERE singleton=1 AND slot_id=?",
                (sent, now, now, slot_id),
            )

    def global_send_status(self, *, now: float) -> dict[str, Any]:
        """Return text-free circuit/spacing state after a reservation refusal."""
        with self._connect() as con:
            circuit = con.execute(
                "SELECT state,reason,generation FROM proactive_global_circuit WHERE singleton=1"
            ).fetchone()
            spacing = con.execute(
                "SELECT reserved_until,last_visible_at FROM proactive_global_send WHERE singleton=1"
            ).fetchone()
        available_at = now
        if spacing is not None:
            if spacing["reserved_until"] is not None:
                available_at = max(available_at, float(spacing["reserved_until"]))
            if spacing["last_visible_at"] is not None:
                available_at = max(available_at, float(spacing["last_visible_at"]) + 300.0)
        return {
            "circuit_state": str(circuit["state"]) if circuit else "closed",
            "circuit_reason": str(circuit["reason"]) if circuit else "",
            "circuit_generation": int(circuit["generation"]) if circuit else 0,
            "available_at": available_at,
        }

    def open_circuit(self, reason: str, *, now: float) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO proactive_global_circuit VALUES(1,'open',?,1,?) ON CONFLICT(singleton) DO UPDATE SET state='open',reason=excluded.reason,generation=proactive_global_circuit.generation+1,updated_at=excluded.updated_at",
                (reason[:120], now),
            )

    def open_probe_circuit(self, reason: str, *, now: float) -> bool:
        """Open or refresh a transient probe latch without replacing a hard safety latch."""
        allowed = {"model_probe_unavailable", "alarm_sink_probe_unavailable"}
        if reason not in allowed:
            raise ValueError("probe circuit reason must be a transient readiness failure")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                "SELECT state,reason FROM proactive_global_circuit WHERE singleton=1"
            ).fetchone()
            if current is not None and current["state"] == "open" and current["reason"] not in allowed:
                con.execute("ROLLBACK")
                return False
            con.execute(
                "INSERT INTO proactive_global_circuit VALUES(1,'open',?,1,?) ON CONFLICT(singleton) DO UPDATE SET state='open',reason=excluded.reason,generation=proactive_global_circuit.generation+1,updated_at=excluded.updated_at",
                (reason, now),
            )
            con.execute("COMMIT")
        return True

    def operator_reset_circuit(self, *, confirmed: bool, now: float) -> None:
        if not confirmed:
            raise PermissionError("explicit operator confirmation required")
        with self._connect() as con:
            con.execute(
                "INSERT INTO proactive_global_circuit VALUES(1,'closed','operator_reset',1,?) ON CONFLICT(singleton) DO UPDATE SET state='closed',reason='operator_reset',generation=proactive_global_circuit.generation+1,updated_at=excluded.updated_at",
                (now,),
            )

    def recover_probe_circuit(
        self, *, model_ready: bool, alarm_ready: bool,
        cooldown_seconds: int, now: float,
    ) -> bool:
        """Close only a cooled-down transient readiness latch after both probes recover."""
        if not (model_ready and alarm_ready):
            return False
        cutoff = now - max(0, int(cooldown_seconds))
        with self._connect() as con:
            changed = con.execute(
                """UPDATE proactive_global_circuit
                   SET state='closed',reason='probe_auto_recovered',
                       generation=generation+1,updated_at=?
                   WHERE singleton=1 AND state='open'
                     AND reason IN ('model_probe_unavailable','alarm_sink_probe_unavailable')
                     AND updated_at<=?""",
                (now, cutoff),
            ).rowcount
        return bool(changed)


class ProactiveMode(str, Enum):
    DISABLED = "disabled"
    OBSERVE = "observe"
    LIVE = "live"


_EXACT_ALLOWLIST = frozenset({("poke", "kosta-owner", "owner"), ("guest", "stephen-lucier", "guest")})


@dataclass(frozen=True)
class ProactiveConfig:
    enabled: bool = False
    dry_run: bool = True
    mode: ProactiveMode = ProactiveMode.OBSERVE
    transport_owner_profile: str = "poke"
    allowed_contacts: tuple[tuple[str, str, str], ...] = ()
    timezone: str = "UTC"
    active_start: str = "09:00"
    active_end: str = "21:30"
    weekly_interest_cap: int = 2
    weekly_total_cap: int = 3
    min_gap_hours: float = 48.0
    eligibility_min_messages_14d: int = 5
    backoff_after_dismissals: int = 3
    backoff_days: int = 30
    exploration_floor: float = 0.10
    claim_seconds: int = 300
    serious_suppression_hours: float = 72.0
    max_retry_attempts: int = 3
    retry_base_seconds: int = 300
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_seconds: int = 21_600
    kill_generation: int = 0
    alarm_sink_configured: bool = False
    alarm_sink_type: str = ""
    alarm_sink_target: str = ""
    alarm_probe_max_age_seconds: int = 25_200

    def validate(self) -> None:
        if self.mode is ProactiveMode.LIVE and not self.enabled:
            raise ValueError("live mode requires enabled=true")
        if self.transport_owner_profile != "poke":
            raise ValueError("Poke must own proactive transport")
        if self.enabled and self.mode in {ProactiveMode.OBSERVE, ProactiveMode.LIVE} and frozenset(self.allowed_contacts) != _EXACT_ALLOWLIST:
            raise ValueError("proactive allowlist must contain exactly Kosta owner and Stephen")
        if self.min_gap_hours < 48:
            raise ValueError("min_gap_hours cannot be below 48")
        if self.weekly_interest_cap > self.weekly_total_cap:
            raise ValueError("weekly_interest_cap cannot exceed weekly_total_cap")
        if not (0.0 <= self.exploration_floor <= 1.0):
            raise ValueError("exploration_floor must be in [0, 1]")
        if self.alarm_sink_configured and (self.alarm_sink_type or self.alarm_sink_target) and (
            self.alarm_sink_type != "hermes_cron" or not self.alarm_sink_target
            or self.alarm_sink_target in {"local", "origin", "all"}
        ):
            raise ValueError("alarm sink must be hermes_cron with one explicit operator target")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ProactiveConfig":
        raw = raw or {}
        if isinstance(raw.get("agent"), Mapping):
            nested = raw.get("agent", {}).get("proactive", {})
            raw = nested if isinstance(nested, Mapping) else {}
        active_raw = raw.get("active_hours")
        active: Mapping[str, Any] = active_raw if isinstance(active_raw, Mapping) else {}
        try:
            mode = ProactiveMode(str(raw.get("mode") or "disabled").strip().lower())
        except ValueError:
            mode = ProactiveMode.DISABLED
        allowlist = []
        values = raw.get("allowed_contacts", ())
        for item in values if isinstance(values, Sequence) else ():
            if isinstance(item, Mapping):
                allowlist.append((str(item.get("profile") or ""), str(item.get("contact_id") or ""), str(item.get("principal") or "")))
        alarm_sink = raw.get("alarm_sink")
        config = cls(
            enabled=bool(raw.get("enabled", False)),
            dry_run=mode is not ProactiveMode.LIVE,
            mode=mode,
            transport_owner_profile=str(raw.get("transport_owner_profile") or "poke"),
            allowed_contacts=tuple(allowlist),
            timezone=str(raw.get("timezone") or "UTC"),
            active_start=str(active.get("start") or "09:00"),
            active_end=str(active.get("end") or "21:30"),
            weekly_interest_cap=max(0, int(raw.get("weekly_interest_cap", 2))),
            weekly_total_cap=max(0, int(raw.get("weekly_total_cap", 3))),
            min_gap_hours=max(0.0, float(raw.get("min_gap_hours", 48))),
            eligibility_min_messages_14d=max(0, int(raw.get("eligibility_min_messages_14d", 5))),
            backoff_after_dismissals=max(1, int(raw.get("backoff_after_dismissals", 3))),
            backoff_days=max(1, int(raw.get("backoff_days", 30))),
            exploration_floor=min(1.0, max(0.0, float(raw.get("exploration_floor", 0.10)))),
            claim_seconds=max(30, int(raw.get("claim_seconds", raw.get("claim_lease_seconds", 300)))),
            serious_suppression_hours=max(
                0.0,
                float(raw.get("serious_suppression_hours", raw.get("serious_share_block_hours", 72))),
            ),
            max_retry_attempts=max(1, int(raw.get("max_retry_attempts", 3))),
            retry_base_seconds=max(60, int(raw.get("retry_base_seconds", 300))),
            circuit_breaker_failures=max(2, int(raw.get("circuit_breaker_failures", 3))),
            circuit_breaker_cooldown_seconds=max(300, int(raw.get("circuit_breaker_cooldown_seconds", 21_600))),
            kill_generation=max(0, int(raw.get("kill_generation", 0))),
            alarm_sink_configured=bool(
                isinstance(alarm_sink, Mapping) and alarm_sink.get("configured") is True
                and str(alarm_sink.get("type") or "").strip() == "hermes_cron"
                and str(alarm_sink.get("target") or "").strip()
            ),
            alarm_sink_type=str(alarm_sink.get("type") or "").strip() if isinstance(alarm_sink, Mapping) else "",
            alarm_sink_target=str(alarm_sink.get("target") or "").strip() if isinstance(alarm_sink, Mapping) else "",
            alarm_probe_max_age_seconds=max(300, int(
                alarm_sink.get("probe_max_age_seconds", 25_200)
                if isinstance(alarm_sink, Mapping) else 25_200
            )),
        )
        config.validate()
        return config


@dataclass(frozen=True)
class ContactRoute:
    contact_id: str
    profile_name: str
    timezone: str = "UTC"
    principal: str = "owner"
    chat_type: str = "dm"
    chat_id: str = ""
    user_id: str = ""
    session_id: str = ""

    @property
    def contact_hash(self) -> str:
        return Path(opaque_contact_filename(self.contact_id)).stem

    def as_dict(self) -> dict[str, str]:
        return {
            "platform": "bluebubbles", "chat_type": self.chat_type,
            "chat_id": self.chat_id, "user_id": self.user_id,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class SlotClaim:
    slot_id: str
    contact_hash: str
    kind: str
    interest_id: str | None
    payload: dict[str, Any]
    fire_at: float
    inbound_version: int
    claim_token: str
    ingress_sequence: int = 0


@dataclass(frozen=True)
class TopicSelection:
    interest: Interest
    kind: ProactiveSendKind
    exploration_probability: float


@dataclass(frozen=True)
class PreparedOutput:
    composed_text: str
    optional_image_url: str | None = None
    reason: str = "prepared_for_async_transport"
    status: str = "prepared"


def assert_unique_contact_ownership(routes: Iterable[ContactRoute]) -> None:
    owners: dict[str, str] = {}
    for route in routes:
        if not str(route.contact_id).strip():
            raise ValueError("contact_id is required")
        if str(route.chat_type).lower() not in _DIRECT_TYPES:
            raise ValueError("proactive scheduling is DM-only; groups are refused")
        previous = owners.setdefault(route.contact_hash, route.profile_name)
        if previous != route.profile_name:
            raise ValueError(
                f"contact profile conflict for {route.contact_hash}: {previous} vs {route.profile_name}"
            )


def _seed(*parts: object) -> int:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8", "replace")
    return int.from_bytes(hashlib.blake2b(material, digest_size=16).digest(), "big")


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class StateContact:
    contact_key: str
    contact_id: str
    profile: str
    route: dict[str, Any]
    timezone_name: str
    last_inbound_at: float | None
    serious_until: float | None
    pending_checkin_kind: str | None
    pending_checkin_reason: str | None
    backoff_until: float | None
    inbound_version: int


class ProactiveStateStore:
    """Low-level durable slot API used by scheduler workers and recovery tests."""

    def __init__(self, state_db: str | Path, *, timeout: float = 10.0) -> None:
        self.path = Path(state_db).expanduser().resolve()
        self.timeout = timeout
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as con:
            _initialize_schema(con)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @staticmethod
    def contact_key(contact_id: str) -> str:
        return Path(opaque_contact_filename(contact_id)).stem

    def register_inbound(
        self, *, profile: str, contact_id: str, route: Mapping[str, Any],
        timezone_name: str, source_id: str, received_at: float,
        serious: bool = False, checkin_kind: str | None = None,
        checkin_reason: str | None = None,
    ) -> dict[str, Any]:
        if str(route.get("chat_type") or "").lower() != "dm":
            raise ValueError("proactive routing is DM-only")
        if str(route.get("platform") or "").lower() != "bluebubbles":
            raise ValueError("proactive routing is bluebubbles-only")
        from zoneinfo import ZoneInfo
        ZoneInfo(timezone_name)
        key, timestamp = self.contact_key(contact_id), _finite(received_at, "received_at")
        con = self._connect()
        con.execute("BEGIN IMMEDIATE")
        try:
            prior = con.execute(
                "SELECT profile_name FROM proactive_contact WHERE contact_hash=?", (key,)
            ).fetchone()
            if prior is not None and prior["profile_name"] != profile:
                raise RuntimeError("contact ownership conflict across profiles")
            con.execute(
                """INSERT INTO proactive_contact(
                   contact_hash,contact_id,route_json,profile_name,timezone,principal,
                   created_at,updated_at) VALUES(?,?,?,?,?,'owner',?,?)
                   ON CONFLICT(contact_hash) DO UPDATE SET
                     route_json=excluded.route_json,timezone=excluded.timezone,
                     contact_id=excluded.contact_id,updated_at=excluded.updated_at""",
                (key, contact_id, json.dumps(dict(route), sort_keys=True), profile,
                 timezone_name, timestamp, timestamp),
            )
            inserted = con.execute(
                "INSERT OR IGNORE INTO proactive_inbound(contact_hash,message_id,received_at) VALUES(?,?,?)",
                (key, str(source_id), timestamp),
            ).rowcount
            cancelled = 0
            if inserted:
                resolved_checkin = "serious" if serious else checkin_kind
                if resolved_checkin not in {None, "serious", "open_loop"}:
                    raise ValueError("invalid check-in kind")
                until = timestamp + 72 * 3600 if serious else None
                advanced = con.execute(
                    """UPDATE proactive_contact SET inbound_version=inbound_version+1,
                       last_inbound_at=?,
                       serious_until=MAX(COALESCE(serious_until,0),COALESCE(?,0)),
                       pending_checkin_kind=?,pending_checkin_reason=?,updated_at=?
                       WHERE contact_hash=? AND (last_inbound_at IS NULL OR ?>last_inbound_at)""",
                    (timestamp, until, resolved_checkin, checkin_reason, timestamp, key, timestamp),
                ).rowcount
                if advanced:
                    cancelled = con.execute(
                        """UPDATE proactive_slot SET status='cancelled',reason='cancelled_on_inbound',
                           claim_token=NULL,claim_until=NULL,updated_at=?
                           WHERE contact_hash=? AND status IN ('armed','claimed') AND created_at<?""",
                        (timestamp, key, timestamp),
                    ).rowcount
            con.execute("COMMIT")
            return {"inserted": bool(inserted), "cancelled": cancelled}
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def contacts(self) -> list[StateContact]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM proactive_contact ORDER BY contact_hash").fetchall()
        return [StateContact(
            str(row["contact_hash"]), str(row["contact_id"] or ""),
            str(row["profile_name"]), json.loads(row["route_json"] or "{}"),
            str(row["timezone"]),
            float(row["last_inbound_at"]) if row["last_inbound_at"] is not None else None,
            float(row["serious_until"]) if row["serious_until"] else None,
            str(row["pending_checkin_kind"]) if row["pending_checkin_kind"] else None,
            str(row["pending_checkin_reason"]) if row["pending_checkin_reason"] else None,
            float(row["disabled_until"]) if row["disabled_until"] else None,
            int(row["inbound_version"]),
        ) for row in rows]

    def record_ingress_observed(self, message_id: str, *, observed_at: float) -> int:
        source_id = str(message_id).strip()
        if not source_id:
            raise ValueError("message_id is required")
        with self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO proactive_ingress_observed(message_id,observed_at) VALUES(?,?)",
                (source_id, _finite(observed_at, "observed_at")),
            )
            row = con.execute(
                "SELECT rowid FROM proactive_ingress_observed WHERE message_id=?", (source_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("ingress arrival sequence was not durably visible")
            return int(row[0])

    def current_ingress_sequence(self, *, con: sqlite3.Connection | None = None) -> int:
        """Return the durable global arrival sequence in this profile state DB."""
        if con is not None:
            return int(con.execute(
                "SELECT COALESCE(max(rowid),0) FROM proactive_ingress_observed"
            ).fetchone()[0])
        with self._connect() as owned:
            return int(owned.execute(
                "SELECT COALESCE(max(rowid),0) FROM proactive_ingress_observed"
            ).fetchone()[0])

    def set_backoff(self, contact_key: str, until: float) -> None:
        with self._connect() as con:
            con.execute("UPDATE proactive_contact SET disabled_until=?,updated_at=? WHERE contact_hash=?",
                        (until, until, contact_key))

    def clear_pending_checkin(self, contact_key: str, *, now: float) -> None:
        with self._connect() as con:
            con.execute(
                """UPDATE proactive_contact SET pending_checkin_kind=NULL,
                   pending_checkin_reason=NULL,updated_at=? WHERE contact_hash=?""",
                (now, contact_key),
            )

    def has_pending_slot(self, contact_key: str) -> bool:
        with self._connect() as con:
            return con.execute(
                "SELECT 1 FROM proactive_slot WHERE contact_hash=? AND status IN ('armed','claimed') LIMIT 1",
                (contact_key,),
            ).fetchone() is not None

    def arm_slot(
        self, *, contact_key: str, kind: str, interest_id: str | None,
        anchor_at: float, planned_at: float, fire_at: float,
        payload: Mapping[str, Any],
    ) -> str:
        del anchor_at
        slot_id = uuid.uuid4().hex
        con = self._connect()
        con.execute("BEGIN IMMEDIATE")
        try:
            existing = con.execute(
                """SELECT slot_id FROM proactive_slot WHERE contact_hash=?
                   AND status IN ('armed','claimed') ORDER BY created_at LIMIT 1""",
                (contact_key,),
            ).fetchone()
            if existing is not None:
                con.execute("COMMIT")
                return str(existing["slot_id"])
            contact = con.execute(
                "SELECT inbound_version FROM proactive_contact WHERE contact_hash=?", (contact_key,)
            ).fetchone()
            if contact is None:
                raise KeyError("unknown proactive contact")
            con.execute(
                """INSERT INTO proactive_slot(slot_id,contact_hash,kind,interest_id,payload_json,
                   status,fire_at,inbound_version,ingress_sequence,created_at,updated_at)
                   VALUES(?,?,?,?,?,'armed',?,?,?,?,?)""",
                (slot_id, contact_key, kind, interest_id,
                 json.dumps(dict(payload), sort_keys=True), fire_at,
                 int(contact["inbound_version"]), self.current_ingress_sequence(con=con),
                 planned_at, planned_at),
            )
            con.execute("COMMIT")
            return slot_id
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    @staticmethod
    def _claim(row: sqlite3.Row) -> SlotClaim:
        return SlotClaim(
            str(row["slot_id"]), str(row["contact_hash"]), str(row["kind"]),
            str(row["interest_id"]) if row["interest_id"] else None,
            json.loads(row["payload_json"] or "{}"), float(row["fire_at"]),
            int(row["inbound_version"]), str(row["claim_token"]),
            int(row["ingress_sequence"] or 0),
        )

    def claim_due(self, *, now: float, lease_seconds: int) -> SlotClaim | None:
        con = self._connect()
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """UPDATE proactive_slot SET status='armed',claim_token=NULL,claim_until=NULL,
                   reason='claim_expired',updated_at=? WHERE status='claimed' AND claim_until<=?""",
                (now, now),
            )
            con.execute(
                """UPDATE proactive_slot SET status='cancelled',reason='stale',
                   claim_token=NULL,claim_until=NULL,updated_at=?
                   WHERE status IN ('armed','claimed') AND fire_at<?""", (now, now - _DAY),
            )
            row = con.execute(
                "SELECT * FROM proactive_slot WHERE status='armed' AND fire_at<=? ORDER BY fire_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                con.execute("COMMIT")
                return None
            token = uuid.uuid4().hex
            con.execute(
                "UPDATE proactive_slot SET status='claimed',claim_token=?,claim_until=?,updated_at=? WHERE slot_id=?",
                (token, now + lease_seconds, now, row["slot_id"]),
            )
            claimed = con.execute("SELECT * FROM proactive_slot WHERE slot_id=?", (row["slot_id"],)).fetchone()
            con.execute("COMMIT")
            return self._claim(claimed)
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def claim_is_current(self, claim: SlotClaim) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT status,claim_token FROM proactive_slot WHERE slot_id=?", (claim.slot_id,)
            ).fetchone()
        return bool(row and row["status"] == "claimed" and row["claim_token"] == claim.claim_token)

    def finish_claim(self, claim: SlotClaim, *, now: float, status: str = "fired") -> bool:
        with self._connect() as con:
            return bool(con.execute(
                """UPDATE proactive_slot SET status=?,claim_token=NULL,claim_until=NULL,updated_at=?
                   WHERE slot_id=? AND status='claimed' AND claim_token=?""",
                (status, now, claim.slot_id, claim.claim_token),
            ).rowcount)

    def slot(self, slot_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM proactive_slot WHERE slot_id=?", (slot_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["status_reason"] = value.get("reason")
        return value


class ProactiveScheduler:
    """SQLite-backed scheduler bound to one explicit profile ``state.db``."""

    def __init__(
        self,
        *,
        state_db_path: str | Path | None = None,
        profile_home: str | Path | None = None,
        profile_name: str | None = None,
        state_db: str | Path | None = None,
        contact_memory_root: str | Path | None = None,
        profile: str | None = None,
        config: ProactiveConfig | Mapping[str, Any] | None = None,
        ownership_routes: Sequence[ContactRoute] = (),
        ownership_registry_path: str | Path | None = None,
        timeout: float = 10.0,
    ) -> None:
        resolved_db = state_db_path or state_db
        if resolved_db is None:
            raise ValueError("explicit profile state.db is required")
        self.state_db_path = Path(resolved_db).expanduser().resolve()
        if profile_home is None:
            profile_home = self.state_db_path.parent
        self.profile_home = Path(profile_home).expanduser().resolve()
        if self.state_db_path != (self.profile_home / "state.db").resolve():
            raise ValueError("scheduler requires the explicit profile state.db path")
        self.profile_name = str(profile_name or profile or "").strip()
        if not self.profile_name:
            raise ValueError("profile_name is required")
        self.config = config if isinstance(config, ProactiveConfig) else ProactiveConfig.from_mapping(config)
        self.config.validate()
        self.timeout = float(timeout)
        self.ownership_registry = ProactiveOwnershipRegistry(
            ownership_registry_path or _default_ownership_registry(self.profile_home),
            timeout=self.timeout,
        )
        self.contact_memory_root = Path(
            contact_memory_root or (self.profile_home / "contact-memory")
        ).expanduser().resolve()
        if self.contact_memory_root != (self.profile_home / "contact-memory").resolve():
            raise ValueError("contact-memory root must belong to explicit profile home")
        assert_unique_contact_ownership(ownership_routes)
        for route in ownership_routes:
            if route.profile_name == self.profile_name:
                continue
            # Routes belonging to another profile are allowed in the ownership
            # audit, but this scheduler may never register/arm them.
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.state_db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        con = sqlite3.connect(self.state_db_path, timeout=self.timeout, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        return con

    def _initialize(self) -> None:
        with self._connect() as con:
            _initialize_schema(con)
            contacts = con.execute(
                "SELECT contact_hash,profile_name FROM proactive_contact"
            ).fetchall()
        for contact in contacts:
            if str(contact["profile_name"]) != self.profile_name:
                raise RuntimeError(
                    f"state.db contains foreign proactive contact for {contact['profile_name']}"
                )
            self.ownership_registry.claim(
                str(contact["contact_hash"]), str(contact["profile_name"]),
                self.state_db_path, now=time.time(),
            )

    def _begin(self) -> sqlite3.Connection:
        con = self._connect()
        con.execute("BEGIN IMMEDIATE")
        return con

    @staticmethod
    def _finish(con: sqlite3.Connection, error: BaseException | None = None) -> None:
        try:
            con.execute("ROLLBACK" if error else "COMMIT")
        finally:
            con.close()

    def register_contact(self, route: ContactRoute, *, now: float | None = None) -> str:
        assert_unique_contact_ownership([route])
        if route.profile_name != self.profile_name:
            raise ValueError("contact route belongs to a different profile")
        if route.principal not in {"owner", "guest"}:
            raise ValueError("principal must be owner or guest")
        if self.config.enabled and (
            route.profile_name, route.contact_id, route.principal
        ) not in self.config.allowed_contacts:
            raise ValueError("contact is not in the exact proactive allowlist")
        # Validate through the check-in zoneinfo path without using host local time.
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(route.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown contact timezone: {route.timezone}") from exc
        timestamp = _finite(time.time() if now is None else now, "now")
        self.ownership_registry.claim(
            route.contact_hash, route.profile_name, self.state_db_path, now=timestamp
        )
        con = self._begin()
        try:
            row = con.execute(
                "SELECT profile_name FROM proactive_contact WHERE contact_hash=?",
                (route.contact_hash,),
            ).fetchone()
            if row is not None and row["profile_name"] != route.profile_name:
                raise ValueError("contact is already owned by another profile")
            con.execute(
                """INSERT INTO proactive_contact(
                   contact_hash,contact_id,route_json,profile_name,timezone,principal,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(contact_hash) DO UPDATE SET
                     contact_id=excluded.contact_id,route_json=excluded.route_json,
                     timezone=excluded.timezone,principal=excluded.principal,updated_at=excluded.updated_at""",
                (
                    route.contact_hash, route.contact_id,
                    json.dumps(route.as_dict(), sort_keys=True),
                    route.profile_name, route.timezone, route.principal, timestamp, timestamp,
                ),
            )
            self._finish(con)
        except BaseException as exc:
            self._finish(con, exc)
            raise
        return route.contact_hash

    def _contact_store(self, route: ContactRoute) -> ContactMemoryStore:
        return ContactMemoryStore(self.profile_home / "contact-memory", route.contact_id)

    def note_inbound(
        self,
        route: ContactRoute,
        *,
        message_id: str,
        received_at: float | None = None,
        serious: bool = False,
        checkin_kind: str | None = None,
        checkin_reason: str | None = None,
    ) -> dict[str, Any]:
        """Record a real inbound and atomically cancel every pending slot."""
        timestamp = _finite(time.time() if received_at is None else received_at, "received_at")
        contact_hash = self.register_contact(route, now=timestamp)
        self._import_ledger_actions(route)
        message_key = str(message_id or "").strip()
        if not message_key:
            raise ValueError("message_id is required for inbound idempotency")
        con = self._begin()
        try:
            inserted = con.execute(
                "INSERT OR IGNORE INTO proactive_inbound(contact_hash,message_id,received_at) VALUES(?,?,?)",
                (contact_hash, message_key, timestamp),
            ).rowcount
            cancelled = 0
            pending_action = None
            if inserted:
                resolved_checkin = "serious" if serious else checkin_kind
                if resolved_checkin not in {None, "serious", "open_loop"}:
                    raise ValueError("invalid check-in kind")
                serious_until = timestamp + self.config.serious_suppression_hours * 3600 if serious else None
                advanced = con.execute(
                    """UPDATE proactive_contact SET inbound_version=inbound_version+1,
                       last_inbound_at=?,
                       serious_until=MAX(COALESCE(serious_until,0),COALESCE(?,0)),
                       pending_checkin_kind=?,pending_checkin_reason=?,updated_at=?
                       WHERE contact_hash=? AND (last_inbound_at IS NULL OR ?>last_inbound_at)""",
                    (timestamp, serious_until, resolved_checkin, checkin_reason,
                     timestamp, contact_hash, timestamp),
                ).rowcount
                if advanced:
                    cancelled = con.execute(
                        """UPDATE proactive_slot SET status='cancelled',reason='cancelled_on_inbound',
                           claim_token=NULL,claim_until=NULL,updated_at=?
                           WHERE contact_hash=? AND status IN ('armed','claimed') AND created_at<?""",
                        (timestamp, contact_hash, timestamp),
                    ).rowcount
            pending_action = con.execute(
                """SELECT * FROM proactive_action WHERE contact_hash=? AND status='sent'
                   AND sent_at<=? AND sent_at>=? ORDER BY sent_at DESC LIMIT 1""",
                (contact_hash, timestamp, timestamp - _DAY),
            ).fetchone()
            self._finish(con)
        except BaseException as exc:
            self._finish(con, exc)
            raise
        return {
            "inserted": bool(inserted),
            "cancelled": int(cancelled),
            "pending_action": dict(pending_action) if pending_action is not None else None,
        }

    def inbound_count(self, contact_hash: str, *, now: float | None = None) -> int:
        timestamp = _finite(time.time() if now is None else now, "now")
        with self._connect() as con:
            return int(con.execute(
                "SELECT count(*) FROM proactive_inbound WHERE contact_hash=? AND received_at>=?",
                (contact_hash, timestamp - 14 * _DAY),
            ).fetchone()[0])

    def _eligibility_reason_in(self, con: sqlite3.Connection, contact_hash: str, kind: str, now: float) -> str | None:
        contact = con.execute(
            "SELECT * FROM proactive_contact WHERE contact_hash=?", (contact_hash,)
        ).fetchone()
        if contact is None or contact["profile_name"] != self.profile_name:
            return "unknown_or_foreign_contact"
        inbound_count = int(con.execute(
            "SELECT count(*) FROM proactive_inbound WHERE contact_hash=? AND received_at>=?",
            (contact_hash, now - 14 * _DAY),
        ).fetchone()[0])
        if inbound_count < self.config.eligibility_min_messages_14d:
            return "insufficient_recent_messages"
        if contact["disabled_until"] is not None and float(contact["disabled_until"]) > now:
            return "backoff_active"
        if kind in {"interest_share", "exploration"} and contact["serious_until"] is not None and float(contact["serious_until"]) > now:
            return "serious_mode"
        actions = con.execute(
            """SELECT kind,COALESCE(sent_at,created_at) AS effective_at,outcome,status
               FROM proactive_action WHERE contact_hash=? AND status IN ('sent','dry_run')
               AND COALESCE(sent_at,created_at)>=? ORDER BY effective_at DESC""",
            (contact_hash, now - _WEEK),
        ).fetchall()
        if len(actions) >= self.config.weekly_total_cap:
            return "weekly_total_cap"
        if kind in {"interest_share", "exploration"} and sum(
            row["kind"] in {"interest_share", "exploration"} for row in actions
        ) >= self.config.weekly_interest_cap:
            return "weekly_interest_cap"
        latest = actions[0] if actions else con.execute(
            """SELECT COALESCE(sent_at,created_at) AS effective_at,outcome,kind,status
               FROM proactive_action WHERE contact_hash=? AND status IN ('sent','dry_run')
               ORDER BY effective_at DESC LIMIT 1""",
            (contact_hash,),
        ).fetchone()
        if latest is not None and now - float(latest["effective_at"]) < self.config.min_gap_hours * 3600:
            return "minimum_gap"
        unanswered = con.execute(
            """SELECT 1 FROM proactive_action a JOIN proactive_contact c USING(contact_hash)
               WHERE a.contact_hash=? AND a.status IN ('sent','dry_run') AND a.outcome IS NULL
               AND COALESCE(a.sent_at,a.created_at)>=COALESCE(c.last_inbound_at,0) LIMIT 1""",
            (contact_hash,),
        ).fetchone()
        if unanswered is not None:
            return "one_strike_unanswered"
        return None

    def eligibility_reason(self, contact_hash: str, kind: str, *, now: float | None = None) -> str | None:
        timestamp = _finite(time.time() if now is None else now, "now")
        with self._connect() as con:
            return self._eligibility_reason_in(con, contact_hash, kind, timestamp)

    def arm_slot(
        self,
        route: ContactRoute,
        *,
        kind: ProactiveSendKind | str,
        fire_at: float,
        interest_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        now: float | None = None,
        slot_id: str | None = None,
    ) -> str:
        if not self.config.enabled:
            raise RuntimeError("proactive scheduling is disabled")
        timestamp = _finite(time.time() if now is None else now, "now")
        fire = _finite(fire_at, "fire_at")
        resolved_kind = ProactiveSendKind(str(getattr(kind, "value", kind))).value
        contact_hash = self.register_contact(route, now=timestamp)
        con = self._begin()
        try:
            existing = con.execute(
                """SELECT slot_id FROM proactive_slot WHERE contact_hash=?
                   AND status IN ('armed','claimed') ORDER BY created_at LIMIT 1""",
                (contact_hash,),
            ).fetchone()
            if existing is not None:
                identifier = str(existing["slot_id"])
                self._finish(con)
                return identifier
            reason = self._eligibility_reason_in(con, contact_hash, resolved_kind, timestamp)
            if reason:
                raise ValueError(f"contact is not eligible: {reason}")
            contact = con.execute(
                "SELECT inbound_version FROM proactive_contact WHERE contact_hash=?", (contact_hash,)
            ).fetchone()
            identifier = slot_id or uuid.uuid4().hex
            con.execute(
                """INSERT INTO proactive_slot(
                   slot_id,contact_hash,kind,interest_id,payload_json,status,fire_at,
                   inbound_version,ingress_sequence,created_at,updated_at)
                   VALUES(?,?,?,?,?,'armed',?,?,?,?,?)""",
                (
                    identifier, contact_hash, resolved_kind, interest_id,
                    json.dumps(dict(payload or {}), sort_keys=True, ensure_ascii=False),
                    fire, int(contact["inbound_version"]),
                    int(con.execute("SELECT COALESCE(max(rowid),0) FROM proactive_ingress_observed").fetchone()[0]),
                    timestamp, timestamp,
                ),
            )
            self._finish(con)
        except BaseException as exc:
            self._finish(con, exc)
            raise
        return identifier

    def arm_operator_smoke(
        self,
        route: ContactRoute,
        *,
        route_commitment: str,
        candidate_override: object | None = None,
        replace_armed_slot: bool = False,
        now: float | None = None,
        slot_id: str | None = None,
    ) -> str:
        """Arm one audited immediate smoke for an exact stored DM route."""
        timestamp = _finite(time.time() if now is None else now, "now")
        if not self.config.enabled:
            raise RuntimeError("proactive scheduling is disabled")
        assert_unique_contact_ownership((route,))
        if str(route.chat_type).lower() not in _DIRECT_TYPES:
            raise ValueError("operator smoke is DM-only")
        if (route.profile_name, route.contact_id, route.principal) not in self.config.allowed_contacts:
            raise ValueError("operator smoke route is not in the exact allowlist")
        commitment = str(route_commitment or "").strip()
        if not commitment:
            raise ValueError("operator smoke route commitment is required")
        contact_hash = route.contact_hash
        interests = self._contact_store(route).eligible_interests(now=timestamp)
        if not interests:
            raise ValueError("operator smoke has no eligible interest")
        interest = sorted(
            interests,
            key=lambda item: (item.effective_score(timestamp), item.interest_id),
            reverse=True,
        )[0]
        candidate_json: str | None = None
        if candidate_override is not None:
            from gateway.proactive_fetch import ProactiveCandidate

            candidate_json = ProactiveCandidate.parse(candidate_override).to_json()
        con = self._begin()
        try:
            contact = con.execute(
                "SELECT route_json,inbound_version FROM proactive_contact "
                "WHERE contact_hash=? AND profile_name=? AND contact_id=?",
                (contact_hash, route.profile_name, route.contact_id),
            ).fetchone()
            if contact is None:
                raise ValueError("operator smoke route is not registered")
            stored_route = json.loads(contact["route_json"] or "{}")
            supplied_route = route.as_dict()
            if any(
                str(stored_route.get(key) or "") != str(supplied_route.get(key) or "")
                for key in ("chat_type", "chat_id", "user_id", "session_id")
            ):
                raise ValueError("operator smoke route does not match the stored existing DM")
            if commitment != self.operator_route_commitment(stored_route):
                raise ValueError("operator smoke route commitment does not match the existing DM")
            live_slot = con.execute(
                "SELECT slot_id,status,payload_json FROM proactive_slot WHERE contact_hash=? "
                "AND status IN ('armed','claimed')",
                (contact_hash,),
            ).fetchone()
            if live_slot is not None:
                live_payload = json.loads(live_slot["payload_json"] or "{}")
                if (
                    not replace_armed_slot
                    or live_slot["status"] != "armed"
                    or live_payload.get("operator_smoke") is True
                ):
                    raise ValueError("operator smoke contact already has live work")
                con.execute(
                    """UPDATE proactive_slot SET status='cancelled',
                       reason='replaced_by_operator_smoke',updated_at=? WHERE slot_id=?""",
                    (timestamp, live_slot["slot_id"]),
                )
            identifier = slot_id or f"operator-smoke-{uuid.uuid4().hex}"
            payload = {
                "topic": interest.topic,
                "session_id": route.session_id,
                "operator_smoke": True,
                "active_hours_override": True,
                "route_commitment": commitment,
            }
            if candidate_json is not None:
                payload["reused_candidate_json"] = candidate_json
            con.execute(
                """INSERT INTO proactive_slot(
                   slot_id,contact_hash,kind,interest_id,payload_json,status,fire_at,
                   inbound_version,ingress_sequence,reason,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'armed',?,?,?,'operator_smoke',?,?)""",
                (
                    identifier, contact_hash, ProactiveSendKind.INTEREST_SHARE.value,
                    interest.interest_id,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False), timestamp,
                    int(contact["inbound_version"]),
                    int(con.execute(
                        "SELECT COALESCE(max(rowid),0) FROM proactive_ingress_observed"
                    ).fetchone()[0]),
                    timestamp, timestamp,
                ),
            )
            self._finish(con)
            return identifier
        except BaseException as exc:
            self._finish(con, exc)
            raise

    @staticmethod
    def operator_route_commitment(route: Mapping[str, Any]) -> str:
        """Return a text-free commitment to one stored existing-DM route."""
        canonical = {
            key: str(route.get(key) or "")
            for key in ("platform", "chat_type", "chat_id", "user_id", "session_id")
        }
        return hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def schedule_checkin(
        self,
        route: ContactRoute,
        *,
        kind: str,
        last_user_ts: float,
        reason: str,
        now: float | None = None,
    ) -> str | None:
        timestamp = _finite(time.time() if now is None else now, "now")
        contact_hash = self.register_contact(route, now=timestamp)
        with self._connect() as con:
            local_day_start = timestamp - _DAY
            already = int(con.execute(
                """SELECT count(*) FROM proactive_action WHERE contact_hash=? AND kind='checkin'
                   AND status IN ('sent','dry_run') AND COALESCE(sent_at,created_at)>=?""",
                (contact_hash, local_day_start),
            ).fetchone()[0])
        plan = plan_checkin(
            thread_key=contact_hash,
            kind=kind,  # type: ignore[arg-type]
            last_user_ts=last_user_ts,
            already_sent_today=already,
            reason=reason,
            timezone=route.timezone,
            active_start=self.config.active_start,
            active_end=self.config.active_end,
        )
        if plan is None:
            return None
        return self.arm_slot(
            route,
            kind=ProactiveSendKind.CHECKIN,
            fire_at=plan.send_at_ts,
            payload={
                "thread_key": plan.thread_key,
                "kind": plan.kind,
                "reason": plan.reason,
                "timezone": plan.timezone,
            },
            now=timestamp,
        )

    def claim_due(self, *, worker_id: str, now: float | None = None, limit: int = 10) -> list[SlotClaim]:
        if not self.config.enabled:
            return []
        timestamp = _finite(time.time() if now is None else now, "now")
        worker = str(worker_id or "").strip()
        if not worker:
            raise ValueError("worker_id is required")
        con = self._begin()
        try:
            # A process loss while transport was in-flight is delivery-unknown,
            # never evidence that it is safe to resend.
            con.execute(
                """UPDATE proactive_delivery SET state='delivery_unknown',last_error_class='process_interrupted',updated_at=?
                   WHERE state='sending' AND slot_id IN
                   (SELECT slot_id FROM proactive_slot WHERE status='claimed' AND claim_until<=?)""",
                (timestamp, timestamp),
            )
            con.execute(
                """UPDATE proactive_slot SET status='suppressed',claim_token=NULL,claim_until=NULL,
                   reason='delivery_unknown',updated_at=? WHERE status='claimed' AND claim_until<=?
                   AND slot_id IN (SELECT slot_id FROM proactive_delivery WHERE state='delivery_unknown')""",
                (timestamp, timestamp),
            )
            # Restart safety: expired leases become armable again.  Very stale
            # slots die silently instead of producing automation-smelling sends.
            con.execute(
                """UPDATE proactive_slot SET status='armed',claim_token=NULL,claim_until=NULL,
                   reason='claim_expired',updated_at=? WHERE status='claimed' AND claim_until<=?""",
                (timestamp, timestamp),
            )
            con.execute(
                """UPDATE proactive_slot SET status='cancelled',reason='stale_after_restart',
                   claim_token=NULL,claim_until=NULL,updated_at=?
                   WHERE status IN ('armed','claimed') AND fire_at<?""",
                (timestamp, timestamp - _DAY),
            )
            rows = con.execute(
                """SELECT s.*,c.inbound_version AS current_inbound,
                   EXISTS(SELECT 1 FROM proactive_inbound i WHERE i.contact_hash=s.contact_hash
                          AND i.received_at>s.created_at) AS newer_inbound
                   FROM proactive_slot s JOIN proactive_contact c USING(contact_hash)
                   WHERE s.status='armed' AND s.fire_at<=?
                   ORDER BY s.fire_at,s.slot_id LIMIT ?""",
                (timestamp, max(0, int(limit)) * 4),
            ).fetchall()
            claims: list[SlotClaim] = []
            for row in rows:
                if len(claims) >= max(0, int(limit)):
                    break
                if bool(row["newer_inbound"]):
                    con.execute(
                        "UPDATE proactive_slot SET status='cancelled',reason='cancelled_on_inbound',updated_at=? WHERE slot_id=?",
                        (timestamp, row["slot_id"]),
                    )
                    continue
                reason = self._eligibility_reason_in(con, row["contact_hash"], row["kind"], timestamp)
                if reason:
                    con.execute(
                        "UPDATE proactive_slot SET status='suppressed',reason=?,updated_at=? WHERE slot_id=?",
                        (reason, timestamp, row["slot_id"]),
                    )
                    continue
                token = hashlib.sha256(
                    f"{worker}\0{row['slot_id']}\0{uuid.uuid4().hex}".encode()
                ).hexdigest()
                changed = con.execute(
                    """UPDATE proactive_slot SET status='claimed',claim_token=?,claim_until=?,updated_at=?
                       WHERE slot_id=? AND status='armed'""",
                    (token, timestamp + self.config.claim_seconds, timestamp, row["slot_id"]),
                ).rowcount
                if changed:
                    claims.append(SlotClaim(
                        str(row["slot_id"]), str(row["contact_hash"]), str(row["kind"]),
                        str(row["interest_id"]) if row["interest_id"] is not None else None,
                        json.loads(row["payload_json"] or "{}"), float(row["fire_at"]),
                        int(row["inbound_version"]), token, int(row["ingress_sequence"] or 0),
                    ))
            self._finish(con)
            return claims
        except BaseException as exc:
            self._finish(con, exc)
            raise

    def complete_claim(
        self,
        claim: SlotClaim,
        *,
        sent: bool,
        reason: str,
        now: float | None = None,
    ) -> str:
        """Complete an observe/suppression lease; live sends use delivery ledger APIs."""
        timestamp = _finite(time.time() if now is None else now, "now")
        if sent and self.config.mode is ProactiveMode.LIVE:
            raise RuntimeError("live completion requires the durable async delivery ledger")
        con = self._begin()
        try:
            row = con.execute(
                """SELECT s.*,c.inbound_version AS current_inbound,
                   EXISTS(SELECT 1 FROM proactive_inbound i WHERE i.contact_hash=s.contact_hash
                          AND i.received_at>s.created_at) AS newer_inbound
                   FROM proactive_slot s JOIN proactive_contact c USING(contact_hash)
                   WHERE slot_id=? AND status='claimed' AND claim_token=?""",
                (claim.slot_id, claim.claim_token),
            ).fetchone()
            if row is None:
                raise ValueError("claim is no longer current")
            if bool(row["newer_inbound"]):
                con.execute(
                    """UPDATE proactive_slot SET status='cancelled',reason='cancelled_on_inbound',
                       claim_token=NULL,claim_until=NULL,updated_at=? WHERE slot_id=?""",
                    (timestamp, claim.slot_id),
                )
                self._finish(con)
                return "cancelled"
            eligibility = self._eligibility_reason_in(
                con, str(row["contact_hash"]), str(row["kind"]), timestamp
            )
            if eligibility:
                sent = False
                reason = eligibility
            status = "dry_run" if sent else "suppressed"
            slot_status = "fired" if status == "dry_run" else "suppressed"
            action_id = claim.slot_id
            con.execute(
                """INSERT OR IGNORE INTO proactive_action(
                   action_id,slot_id,contact_hash,interest_id,kind,status,sent_at,
                   inbound_version,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    action_id, claim.slot_id, row["contact_hash"], row["interest_id"], row["kind"],
                    status, timestamp if status == "sent" else None,
                    int(row["inbound_version"]),
                    str(reason), timestamp,
                ),
            )
            con.execute(
                """UPDATE proactive_slot SET status=?,reason=?,claim_token=NULL,claim_until=NULL,
                   updated_at=? WHERE slot_id=?""",
                (slot_status, str(reason), timestamp, claim.slot_id),
            )
            self._finish(con)
            return status
        except BaseException as exc:
            if con.in_transaction:
                self._finish(con, exc)
            raise

    def retry_claim(self, claim: SlotClaim, *, reason: str, now: float | None = None) -> bool:
        """Return a failed projection/initiation lease to the retryable armed state."""
        timestamp = _finite(time.time() if now is None else now, "now")
        with self._connect() as con:
            return bool(con.execute(
                """UPDATE proactive_slot SET status='armed',reason=?,claim_token=NULL,
                   claim_until=NULL,updated_at=? WHERE slot_id=? AND status='claimed'
                   AND claim_token=?""",
                (str(reason), timestamp, claim.slot_id, claim.claim_token),
            ).rowcount)

    def reserve_delivery(
        self, claim: SlotClaim, text: str, *, image_url: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Durably reserve a unique slot/payload before transport I/O."""
        timestamp = _finite(time.time() if now is None else now, "now")
        prepared_image_url = str(image_url).strip() if image_url else None
        payload_hash = hashlib.sha256(json.dumps(
            {"text": str(text), "image_url": prepared_image_url},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        con = self._begin()
        try:
            row = con.execute("SELECT * FROM proactive_delivery WHERE slot_id=?", (claim.slot_id,)).fetchone()
            if row is not None:
                self._finish(con)
                return dict(row)
            current = con.execute(
                "SELECT 1 FROM proactive_slot WHERE slot_id=? AND status='claimed' AND claim_token=?",
                (claim.slot_id, claim.claim_token),
            ).fetchone()
            if current is None:
                raise ValueError("claim is no longer current")
            con.execute("INSERT INTO proactive_delivery(slot_id,state,payload_hash,prepared_payload,prepared_image_url,kill_generation,updated_at) VALUES(?,'prepared',?,?,?,?,?)",
                        (claim.slot_id, payload_hash, str(text), prepared_image_url,
                         self.config.kill_generation, timestamp))
            self._finish(con)
            return {"state": "prepared", "payload_hash": payload_hash, "prepared_payload": str(text),
                    "prepared_image_url": prepared_image_url,
                    "kill_generation": self.config.kill_generation, "attempt_count": 0, "not_before": None}
        except BaseException as exc:
            if con.in_transaction:
                self._finish(con, exc)
            raise

    def final_delivery_check(self, route: ContactRoute, claim: SlotClaim, *, now: float | None = None) -> str | None:
        """Revalidate code-owned fuses at the last responsible moment."""
        timestamp = _finite(time.time() if now is None else now, "now")
        if not self.config.enabled or self.config.mode is not ProactiveMode.LIVE:
            return "mode_not_live"
        if (route.profile_name, route.contact_id, route.principal) not in self.config.allowed_contacts:
            return "allowlist_mismatch"
        if not self.config.alarm_sink_configured:
            return "alarm_sink_unconfigured"
        if not self.model_probe_is_fresh(now=timestamp):
            return "model_probe_unavailable_or_stale"
        if not self.alarm_sink_probe_is_fresh(now=timestamp):
            return "alarm_sink_probe_unavailable_or_stale"
        if not self.reconcile_confirmed_sends(now=timestamp):
            return "confirmed_send_projection_failed"
        if self.ownership_registry.global_send_status(now=timestamp)["circuit_state"] == "open":
            return "global_circuit_open"
        with self._connect() as con:
            failures = con.execute(
                "SELECT state,updated_at FROM proactive_delivery ORDER BY updated_at DESC LIMIT ?",
                (self.config.circuit_breaker_failures,),
            ).fetchall()
            bad = {"failed", "delivery_unknown", "partial_delivery"}
            if (len(failures) >= self.config.circuit_breaker_failures
                    and all(str(item["state"]) in bad for item in failures)
                    and timestamp - float(failures[0]["updated_at"]) < self.config.circuit_breaker_cooldown_seconds):
                return "transport_circuit_open"
            row = con.execute(
                """SELECT s.status,s.claim_token,s.inbound_version,s.payload_json,
                          c.inbound_version current_inbound,
                          s.ingress_sequence,c.disabled_until,c.timezone,c.route_json,
                          d.kill_generation,
                          (SELECT COALESCE(max(rowid),0) FROM proactive_ingress_observed) current_ingress
                   FROM proactive_slot s
                   JOIN proactive_contact c USING(contact_hash)
                   LEFT JOIN proactive_delivery d USING(slot_id)
                   WHERE s.slot_id=?""", (claim.slot_id,),
            ).fetchone()
        if row is None or row["status"] != "claimed" or row["claim_token"] != claim.claim_token:
            return "claim_not_current"
        if int(row["current_inbound"]) != int(row["inbound_version"]):
            return "newer_inbound"
        if int(row["current_ingress"] or 0) > int(row["ingress_sequence"] or 0):
            return "newer_observed_ingress"
        if row["disabled_until"] is not None and float(row["disabled_until"]) > timestamp:
            return "backoff_active"
        if int(row["kill_generation"] or 0) != self.config.kill_generation:
            return "kill_generation_changed"
        payload = json.loads(row["payload_json"] or "{}")
        operator_smoke = payload.get("operator_smoke") is True
        if operator_smoke and (
            payload.get("active_hours_override") is not True
            or str(payload.get("route_commitment") or "")
            != self.operator_route_commitment(json.loads(row["route_json"] or "{}"))
        ):
            return "operator_smoke_tag_invalid"
        from datetime import datetime
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(timestamp, ZoneInfo(str(row["timezone"])))
        current = local.hour * 60 + local.minute
        start_h, start_m = (int(value) for value in self.config.active_start.split(":", 1))
        end_h, end_m = (int(value) for value in self.config.active_end.split(":", 1))
        if (
            not operator_smoke
            and not (start_h * 60 + start_m <= current <= end_h * 60 + end_m)
        ):
            return "outside_active_hours"
        return self.eligibility_reason(claim.contact_hash, claim.kind, now=timestamp)

    def alarm_sink_probe_is_fresh(self, *, now: float | None = None) -> bool:
        timestamp = _finite(time.time() if now is None else now, "now")
        with self._connect() as con:
            row = con.execute(
                "SELECT value_json,updated_at FROM proactive_health WHERE key='alarm_sink_probe'"
            ).fetchone()
        if row is None or timestamp - float(row["updated_at"]) > self.config.alarm_probe_max_age_seconds:
            return False
        try:
            value = json.loads(row["value_json"] or "{}")
        except (TypeError, ValueError):
            return False
        return bool(
            value.get("ready") is True
            and value.get("type") == self.config.alarm_sink_type
            and value.get("target") == self.config.alarm_sink_target
            and value.get("delivery_ack") is True
        )

    def model_probe_is_fresh(self, *, now: float | None = None) -> bool:
        timestamp = _finite(time.time() if now is None else now, "now")
        with self._connect() as con:
            row = con.execute(
                "SELECT value_json,updated_at FROM proactive_health WHERE key='model_probe'"
            ).fetchone()
        if row is None or timestamp - float(row["updated_at"]) > 3900:
            return False
        try:
            value = json.loads(row["value_json"] or "{}")
        except (TypeError, ValueError):
            return False
        return bool(
            value.get("ready") is True and value.get("sent_request") is True
            and value.get("provider") == "openai-codex"
            and value.get("resolved_model") == "gpt-5.6-sol"
            and value.get("response_model") == "gpt-5.6-sol"
            and value.get("private_history_used") is False
        )

    def begin_atomic_send_fence(self, claim: SlotClaim, *, now: float | None = None) -> tuple[sqlite3.Connection | None, str | None]:
        """Linearize arrival vs transport and consume one attempt.

        The write transaction remains open only across the bounded adapter send.
        Ingress observation uses the same SQLite DB, so an arrival is either
        visible here (and cancels) or is durably ordered after the transport.
        """
        timestamp = _finite(time.time() if now is None else now, "now")
        con = self._begin()
        try:
            row = con.execute(
                """SELECT s.status,s.claim_token,s.ingress_sequence,d.state,d.not_before,d.attempt_count,
                          (SELECT COALESCE(max(rowid),0) FROM proactive_ingress_observed) current_ingress
                   FROM proactive_slot s JOIN proactive_delivery d USING(slot_id) WHERE s.slot_id=?""",
                (claim.slot_id,),
            ).fetchone()
            if row is None or row["status"] != "claimed" or row["claim_token"] != claim.claim_token:
                self._finish(con, RuntimeError("claim_not_current"))
                return None, "claim_not_current"
            if int(row["current_ingress"] or 0) > int(row["ingress_sequence"] or 0):
                self._finish(con, RuntimeError("newer_observed_ingress"))
                return None, "newer_observed_ingress"
            if row["state"] != "sending" or (row["not_before"] is not None and float(row["not_before"]) > timestamp):
                self._finish(con, RuntimeError("attempt_not_available"))
                return None, "attempt_not_available"
            if int(row["attempt_count"]) >= self.config.max_retry_attempts:
                self._finish(con, RuntimeError("attempt_budget_exhausted"))
                return None, "attempt_budget_exhausted"
            con.execute(
                "UPDATE proactive_delivery SET attempt_count=attempt_count+1,updated_at=? WHERE slot_id=?",
                (timestamp, claim.slot_id),
            )
            return con, None
        except BaseException as exc:
            if con.in_transaction:
                self._finish(con, exc)
            raise

    def finish_atomic_send_fence(self, con: sqlite3.Connection) -> None:
        self._finish(con)

    def begin_delivery_attempt(self, claim: SlotClaim, *, now: float | None = None) -> bool:
        timestamp = _finite(time.time() if now is None else now, "now")
        con = self._begin()
        try:
            row = con.execute("SELECT * FROM proactive_delivery WHERE slot_id=?", (claim.slot_id,)).fetchone()
            allowed = bool(row and row["state"] == "sending" and
                           (row["not_before"] is None or float(row["not_before"]) <= timestamp) and
                           int(row["attempt_count"]) < self.config.max_retry_attempts)
            changed = 0
            if allowed:
                changed = con.execute(
                    "UPDATE proactive_delivery SET attempt_count=attempt_count+1,updated_at=? WHERE slot_id=? AND state='sending'",
                    (timestamp, claim.slot_id),
                ).rowcount
            self._finish(con)
            return bool(changed)
        except BaseException as exc:
            self._finish(con, exc)
            raise

    def begin_delivery_preflight(self, claim: SlotClaim, *, now: float | None = None) -> bool:
        """Atomically select one worker for auth/spacing checks without consuming an attempt."""
        timestamp = _finite(time.time() if now is None else now, "now")
        with self._connect() as con:
            return bool(con.execute(
                "UPDATE proactive_delivery SET state='sending',updated_at=? WHERE slot_id=? AND state IN ('prepared','retry_wait') AND (not_before IS NULL OR not_before<=?)",
                (timestamp, claim.slot_id, timestamp),
            ).rowcount)

    def finish_delivery(self, claim: SlotClaim, *, state: str, reason: str,
                        message_id: str | None = None, retryable: bool = False,
                        now: float | None = None) -> str:
        """Finalize exactly once; only definite zero-delivery failures retry."""
        timestamp = _finite(time.time() if now is None else now, "now")
        terminal = {"sent", "suppressed", "delivery_unknown", "partial_delivery", "failed"}
        con = self._begin()
        try:
            row = con.execute(
                """SELECT d.*,s.payload_json FROM proactive_delivery d
                   JOIN proactive_slot s USING(slot_id) WHERE d.slot_id=?""",
                (claim.slot_id,),
            ).fetchone()
            if row is None:
                raise ValueError("delivery was not reserved")
            if row["state"] in terminal:
                self._finish(con)
                return str(row["state"])
            attempts = int(row["attempt_count"])
            if retryable and state == "failed" and attempts < self.config.max_retry_attempts:
                delay = min(self.config.retry_base_seconds * (4 ** max(attempts - 1, 0)), 3600)
                timezone_name = str(con.execute(
                    "SELECT timezone FROM proactive_contact WHERE contact_hash=?", (claim.contact_hash,)
                ).fetchone()[0])
                retry_at = push_into_active_hours(
                    timestamp + delay, timezone_name=timezone_name,
                    active_start=self.config.active_start, active_end=self.config.active_end,
                    jitter_minutes=0, jitter_key=claim.slot_id,
                )
                con.execute("UPDATE proactive_delivery SET state='retry_wait',not_before=?,last_error_class=?,updated_at=? WHERE slot_id=?",
                            (retry_at, reason[:120], timestamp, claim.slot_id))
                con.execute("UPDATE proactive_slot SET status='armed',fire_at=?,claim_token=NULL,claim_until=NULL,reason='transport_retry',updated_at=? WHERE slot_id=?",
                            (retry_at, timestamp, claim.slot_id))
                self._finish(con)
                return "retry_wait"
            final = state if state in terminal else "delivery_unknown"
            con.execute("UPDATE proactive_delivery SET state=?,transport_message_id=?,last_error_class=?,projection_state=?,projection_error=NULL,updated_at=? WHERE slot_id=?",
                        (final, message_id, reason[:120], "pending" if final == "sent" else "not_required", timestamp, claim.slot_id))
            action_status = "sent" if final == "sent" else "suppressed"
            slot_payload = json.loads(row["payload_json"] or "{}")
            action_reason = (
                f"operator_smoke:{reason}"
                if slot_payload.get("operator_smoke") is True
                else reason
            )
            con.execute("INSERT OR IGNORE INTO proactive_action(action_id,slot_id,contact_hash,interest_id,kind,status,sent_at,inbound_version,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (claim.slot_id, claim.slot_id, claim.contact_hash, claim.interest_id, claim.kind,
                         action_status, timestamp if final == "sent" else None, claim.inbound_version, action_reason, timestamp))
            con.execute("UPDATE proactive_slot SET status=?,reason=?,claim_token=NULL,claim_until=NULL,updated_at=? WHERE slot_id=?",
                        ("fired" if final == "sent" else "suppressed", reason, timestamp, claim.slot_id))
            self._finish(con)
            if final == "sent":
                try:
                    self._project_confirmed_send(claim, timestamp=timestamp, reason=reason)
                except Exception as exc:
                    with self._connect() as projection_con:
                        projection_con.execute(
                            "UPDATE proactive_delivery SET projection_state='failed',projection_error=?,updated_at=? WHERE slot_id=?",
                            (type(exc).__name__, timestamp, claim.slot_id),
                        )
                    self.ownership_registry.open_circuit("confirmed_send_projection_failed", now=timestamp)
                    logger.exception("Confirmed proactive send projection failed slot=%s", claim.slot_id)
                else:
                    with self._connect() as projection_con:
                        projection_con.execute(
                            "UPDATE proactive_delivery SET projection_state='complete',projection_error=NULL WHERE slot_id=?",
                            (claim.slot_id,),
                        )
            return final
        except BaseException as exc:
            if con.in_transaction:
                self._finish(con, exc)
            raise

    def defer_prepared_delivery(self, claim: SlotClaim, *, not_before: float, reason: str,
                                now: float | None = None) -> str:
        """Retry global-spacing contention without consuming an attempt or payload."""
        timestamp = _finite(time.time() if now is None else now, "now")
        retry_at = max(timestamp + 1.0, _finite(not_before, "not_before"))
        con = self._begin()
        try:
            row = con.execute(
                "SELECT state,attempt_count,prepared_payload FROM proactive_delivery WHERE slot_id=?", (claim.slot_id,)
            ).fetchone()
            if row is None or row["state"] != "sending" or not row["prepared_payload"]:
                raise ValueError("prepared immutable delivery is unavailable for deferral")
            con.execute(
                "UPDATE proactive_delivery SET state='retry_wait',not_before=?,last_error_class=?,updated_at=? WHERE slot_id=?",
                (retry_at, reason[:120], timestamp, claim.slot_id),
            )
            changed = con.execute(
                "UPDATE proactive_slot SET status='armed',fire_at=?,claim_token=NULL,claim_until=NULL,reason=?,updated_at=? WHERE slot_id=? AND status='claimed' AND claim_token=?",
                (retry_at, reason[:120], timestamp, claim.slot_id, claim.claim_token),
            ).rowcount
            if not changed:
                raise ValueError("claim is no longer current for spacing deferral")
            self._finish(con)
            return "retry_wait"
        except BaseException as exc:
            self._finish(con, exc)
            raise

    def _project_confirmed_send(self, claim: SlotClaim, *, timestamp: float, reason: str) -> None:
        with self._connect() as con:
            row = con.execute(
                "SELECT c.contact_id,s.payload_json,d.payload_hash FROM proactive_slot s JOIN proactive_contact c USING(contact_hash) JOIN proactive_delivery d USING(slot_id) WHERE s.slot_id=?",
                (claim.slot_id,),
            ).fetchone()
        if row is None or not row["contact_id"]:
            raise RuntimeError("confirmed send lacks canonical contact")
        payload = json.loads(row["payload_json"] or "{}")
        candidate = {
            "topic_hash": hashlib.sha256(str(payload.get("topic") or "").encode()).hexdigest(),
            "payload_hash": str(row["payload_hash"]),
        }
        store = ContactMemoryStore(self.profile_home / "contact-memory", str(row["contact_id"]))
        if store.get_proactive_send(claim.slot_id) is None:
            store.record_proactive_send(ProactiveSend(
                send_id=claim.slot_id, interest_id=claim.interest_id,
                kind=ProactiveSendKind(claim.kind),
                candidate_json=json.dumps(candidate, sort_keys=True, separators=(",", ":")),
                gate_decision=GateDecision.SENT, gate_reason=reason,
                sent_at=timestamp, outcome=None, outcome_at=None, created_at=timestamp,
            ))
        from gateway.proactive_fetch import suppression_metrics
        if suppression_metrics(store, since=timestamp - _WEEK).alarm:
            self.ownership_registry.open_circuit("send_rate_above_40_percent", now=timestamp)

    def reconcile_confirmed_sends(self, *, now: float | None = None) -> bool:
        """Repair every durable confirmed-send projection before more transport."""
        timestamp = _finite(time.time() if now is None else now, "now")
        with self._connect() as con:
            rows = con.execute(
                """SELECT s.*,d.last_error_class FROM proactive_slot s JOIN proactive_delivery d USING(slot_id)
                   WHERE d.state='sent' AND d.projection_state!='complete' ORDER BY d.updated_at"""
            ).fetchall()
        ok = True
        for row in rows:
            claim = SlotClaim(
                str(row["slot_id"]), str(row["contact_hash"]), str(row["kind"]),
                str(row["interest_id"]) if row["interest_id"] else None,
                json.loads(row["payload_json"] or "{}"), float(row["fire_at"]),
                int(row["inbound_version"]), str(row["claim_token"] or "reconcile"),
                int(row["ingress_sequence"] or 0),
            )
            try:
                self._project_confirmed_send(
                    claim, timestamp=float(row["updated_at"]),
                    reason=str(row["last_error_class"] or "sent"),
                )
            except Exception as exc:
                ok = False
                with self._connect() as con:
                    con.execute(
                        "UPDATE proactive_delivery SET projection_state='failed',projection_error=? WHERE slot_id=?",
                        (type(exc).__name__, claim.slot_id),
                    )
            else:
                with self._connect() as con:
                    con.execute(
                        "UPDATE proactive_delivery SET projection_state='complete',projection_error=NULL WHERE slot_id=?",
                        (claim.slot_id,),
                    )
        if not ok:
            self.ownership_registry.open_circuit("confirmed_send_projection_failed", now=timestamp)
        return ok

    def enforce_send_rate_circuit(self, *, now: float | None = None) -> dict[str, Any]:
        """Atomically open the durable global circuit when trailing sends exceed 40%."""
        timestamp = _finite(time.time() if now is None else now, "now")
        # state.db is canonical after transport confirmation. The contact
        # ledger is a durable projection and may temporarily be pending.
        with self._connect() as con:
            row = con.execute(
                """SELECT count(*),COALESCE(sum(CASE WHEN status='sent' THEN 1 ELSE 0 END),0)
                   FROM proactive_action WHERE created_at>=?""",
                (timestamp - _WEEK,),
            ).fetchone()
        total, sent = int(row[0]), int(row[1])
        rate = sent / total if total else 0.0
        from gateway.proactive_fetch import MIN_SEND_RATE_SAMPLES
        alarm = total >= MIN_SEND_RATE_SAMPLES and rate > 0.40
        if alarm:
            self.ownership_registry.open_circuit("send_rate_above_40_percent", now=timestamp)
        return {"total": total, "sent": sent, "send_rate": rate,
                "circuit_opened": alarm}

    def record_health(self, key: str, value: Mapping[str, Any], *, now: float | None = None) -> None:
        timestamp = _finite(time.time() if now is None else now, "now")
        with self._connect() as con:
            con.execute("INSERT INTO proactive_health(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                        (str(key), json.dumps(dict(value), sort_keys=True), timestamp))

    def bind_route_fingerprint(self, route: ContactRoute, fingerprint: str) -> bool:
        value = str(fingerprint).strip()
        if not value:
            return False
        con = self._begin()
        try:
            row = con.execute(
                "SELECT profile_name,contact_id,route_fingerprint FROM proactive_contact WHERE contact_hash=?",
                (route.contact_hash,),
            ).fetchone()
            valid = bool(
                row and row["profile_name"] == route.profile_name
                and row["contact_id"] == route.contact_id
                and (row["route_fingerprint"] is None or row["route_fingerprint"] == value)
            )
            if valid and row["route_fingerprint"] is None:
                con.execute(
                    "UPDATE proactive_contact SET route_fingerprint=? WHERE contact_hash=? AND route_fingerprint IS NULL",
                    (value, route.contact_hash),
                )
            self._finish(con)
            return valid
        except BaseException as exc:
            self._finish(con, exc)
            raise

    def earliest_retry_at(self) -> float | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT min(not_before) FROM proactive_delivery WHERE state='retry_wait'"
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    @staticmethod
    def _topic_hash(topic: str) -> str:
        normalized = " ".join(str(topic or "").casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def cache_cancelled_candidate(
        self, claim: Any, candidate: Any, *, now: float | None = None,
    ) -> bool:
        """Retain one compact fetched candidate for exact-topic reuse for 24h."""
        from gateway.proactive_fetch import ProactiveCandidate

        timestamp = _finite(time.time() if now is None else now, "now")
        parsed = ProactiveCandidate.parse(candidate)
        topic = str(getattr(claim, "payload", {}).get("topic") or parsed.topic)
        if not topic or str(getattr(claim, "contact_hash", "")) == "":
            return False
        with self._connect() as con:
            con.execute(
                """INSERT INTO proactive_cancelled_fetch(
                   source_slot_id,contact_hash,topic_hash,candidate_json,cached_at,expires_at
                   ) VALUES(?,?,?,?,?,?) ON CONFLICT(source_slot_id) DO NOTHING""",
                (
                    str(claim.slot_id), str(claim.contact_hash), self._topic_hash(topic),
                    parsed.to_json(), timestamp, timestamp + _DAY,
                ),
            )
            return bool(con.execute("SELECT changes()").fetchone()[0])

    def reusable_cancelled_candidate(
        self, route: ContactRoute, *, topic: str, now: float | None = None,
    ) -> str | None:
        """Atomically consume the newest unexpired exact-topic candidate."""
        timestamp = _finite(time.time() if now is None else now, "now")
        con = self._begin()
        try:
            row = con.execute(
                """SELECT source_slot_id,candidate_json FROM proactive_cancelled_fetch
                   WHERE contact_hash=? AND topic_hash=? AND consumed_at IS NULL
                   AND expires_at>=? ORDER BY cached_at DESC,source_slot_id DESC LIMIT 1""",
                (route.contact_hash, self._topic_hash(topic), timestamp),
            ).fetchone()
            if row is None:
                con.execute(
                    "DELETE FROM proactive_cancelled_fetch WHERE expires_at<?", (timestamp,)
                )
                self._finish(con)
                return None
            changed = con.execute(
                "UPDATE proactive_cancelled_fetch SET consumed_at=? "
                "WHERE source_slot_id=? AND consumed_at IS NULL",
                (timestamp, row["source_slot_id"]),
            ).rowcount
            self._finish(con)
            return str(row["candidate_json"]) if changed else None
        except BaseException as exc:
            self._finish(con, exc)
            raise

    def _claim_is_current(self, claim: SlotClaim) -> bool:
        with self._connect() as con:
            return con.execute(
                "SELECT 1 FROM proactive_slot WHERE slot_id=? AND status='claimed' "
                "AND claim_token=?",
                (claim.slot_id, claim.claim_token),
            ).fetchone() is not None

    def cleanup_sprawl(self, *, now: float | None = None) -> dict[str, int]:
        """Bound operational rows while preserving recent audits and contact evidence."""
        timestamp = _finite(time.time() if now is None else now, "now")
        con = self._begin()
        try:
            redacted = con.execute(
                "UPDATE proactive_delivery SET prepared_payload=NULL WHERE prepared_payload IS NOT NULL AND updated_at<? AND state IN ('sent','suppressed','delivery_unknown','partial_delivery','failed')",
                (timestamp - 30 * 86400,),
            ).rowcount
            counts = {
                "inbound": con.execute("DELETE FROM proactive_inbound WHERE received_at < ?", (timestamp - 365 * 86400,)).rowcount,
                "deliveries": con.execute(
                    "DELETE FROM proactive_delivery WHERE updated_at < ? AND state IN ('sent','suppressed','failed')",
                    (timestamp - 180 * 86400,),
                ).rowcount,
                "cancelled_fetches": con.execute(
                    "DELETE FROM proactive_cancelled_fetch WHERE expires_at < ? OR consumed_at IS NOT NULL",
                    (timestamp,),
                ).rowcount,
            }
            counts["payloads_redacted"] = redacted
            counts["slots"] = con.execute(
                "DELETE FROM proactive_slot WHERE updated_at < ? AND status IN ('fired','cancelled','suppressed') "
                "AND NOT EXISTS (SELECT 1 FROM proactive_delivery d WHERE d.slot_id=proactive_slot.slot_id)",
                (timestamp - 180 * 86400,),
            ).rowcount
            self._finish(con)
            return {key: int(value) for key, value in counts.items()}
        except BaseException as exc:
            self._finish(con, exc)
            raise

    def _total_sent(self, con: sqlite3.Connection, contact_hash: str) -> int:
        return int(con.execute(
            "SELECT count(*) FROM proactive_action WHERE contact_hash=? AND status IN ('sent','dry_run')",
            (contact_hash,),
        ).fetchone()[0])

    def select_topic(
        self,
        route: ContactRoute,
        *,
        now: float | None = None,
        selection_nonce: str = "",
    ) -> TopicSelection | None:
        timestamp = _finite(time.time() if now is None else now, "now")
        contact_hash = self.register_contact(route, now=timestamp)
        if self.eligibility_reason(contact_hash, "interest_share", now=timestamp):
            return None
        store = self._contact_store(route)
        eligible = store.eligible_interests(now=timestamp)
        if not eligible:
            return None
        with self._connect() as con:
            total_sends = self._total_sent(con, contact_hash)
            recent_three = con.execute(
                """SELECT kind FROM proactive_action WHERE contact_hash=? AND status IN ('sent','dry_run')
                   ORDER BY COALESCE(sent_at,created_at) DESC LIMIT 3""",
                (contact_hash,),
            ).fetchall()
        epsilon = max(self.config.exploration_floor, 0.5 * (0.97 ** total_sends))
        rng = random.Random(_seed(contact_hash, int(timestamp // 1800), selection_nonce, total_sends))
        # Thompson exploitation over all eligible interests.
        ranked = sorted(
            eligible,
            key=lambda item: (
                rng.betavariate(item.ts_alpha, item.ts_beta),
                item.effective_score(timestamp), item.interest_id,
            ),
            reverse=True,
        )
        anchor = ranked[0]
        exploration_allowed = not any(row["kind"] == "exploration" for row in recent_three)
        if exploration_allowed and rng.random() < epsilon:
            live_positive = store.list_interests(
                valence=InterestValence.POSITIVE, live_only=True, now=timestamp
            )
            if anchor.parent_id:
                siblings = [
                    item for item in live_positive
                    if item.parent_id == anchor.parent_id and item.interest_id != anchor.interest_id
                ]
            else:
                siblings = [item for item in live_positive if item.parent_id == anchor.interest_id]
            siblings = [item for item in siblings if item.state is not InterestState.RETIRED]
            if siblings:
                siblings.sort(key=lambda item: (item.effective_score(timestamp), item.interest_id), reverse=True)
                return TopicSelection(rng.choice(siblings), ProactiveSendKind.EXPLORATION, epsilon)
        return TopicSelection(anchor, ProactiveSendKind.INTEREST_SHARE, epsilon)

    @staticmethod
    def classify_outcome(text: str, topic: str) -> ProactiveOutcome:
        clean = " ".join(str(text or "").split())
        lowered = clean.casefold()
        if re_search_dismissive(lowered):
            return ProactiveOutcome.DISMISSED
        if re.fullmatch(r"ok(?:ay)?[^\w]*", lowered):
            return ProactiveOutcome.ACKNOWLEDGED
        words = [word for word in lowered.replace("-", " ").split() if word]
        topic_words = {word for word in str(topic or "").casefold().split() if len(word) > 2}
        overlap = bool(topic_words & set(words))
        if len(words) > 5 or overlap or "?" in clean:
            return ProactiveOutcome.ENGAGED
        return ProactiveOutcome.ACKNOWLEDGED

    def set_action_outcome(
        self,
        action_id: str,
        outcome: ProactiveOutcome | str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = _finite(time.time() if now is None else now, "now")
        resolved = ProactiveOutcome(str(getattr(outcome, "value", outcome))).value
        con = self._begin()
        try:
            row = con.execute(
                "SELECT * FROM proactive_action WHERE action_id=? AND status='sent'", (action_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown sent proactive action: {action_id}")
            if row["outcome"] is not None:
                if row["outcome"] != resolved:
                    raise ValueError("proactive action already has a different outcome")
                if row["outcome_status"] == "provisional":
                    raise ValueError("provisional outcome requires confirmation")
                contact = con.execute(
                    "SELECT negative_streak,disabled_until FROM proactive_contact WHERE contact_hash=?",
                    (row["contact_hash"],),
                ).fetchone()
                self._finish(con)
                return {
                    "outcome": resolved,
                    "negative_streak": int(contact["negative_streak"]),
                    "disabled_until": contact["disabled_until"],
                    "applied": False,
                }
            con.execute(
                """UPDATE proactive_action SET outcome=?,outcome_at=?,outcome_status='confirmed'
                   WHERE action_id=? AND outcome IS NULL""",
                (resolved, timestamp, action_id),
            )
            contact = con.execute(
                "SELECT negative_streak,disabled_until FROM proactive_contact WHERE contact_hash=?",
                (row["contact_hash"],),
            ).fetchone()
            streak = int(contact["negative_streak"])
            streak = streak + 1 if resolved in _NEGATIVE_OUTCOMES else 0
            disabled_until = (
                timestamp + self.config.backoff_days * _DAY
                if streak >= self.config.backoff_after_dismissals and contact["disabled_until"] is None
                else contact["disabled_until"]
            )
            con.execute(
                """UPDATE proactive_contact SET negative_streak=?,
                   disabled_until=CASE WHEN ? IS NULL THEN disabled_until ELSE ? END,updated_at=?
                   WHERE contact_hash=?""",
                (streak, disabled_until, disabled_until, timestamp, row["contact_hash"]),
            )
            self._finish(con)
            return {
                "outcome": resolved, "negative_streak": streak,
                "disabled_until": disabled_until, "applied": True,
            }
        except BaseException as exc:
            self._finish(con, exc)
            raise

    def record_provisional_outcome(
        self,
        action_id: str,
        outcome: ProactiveOutcome | str,
        *,
        source_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = _finite(time.time() if now is None else now, "now")
        resolved = ProactiveOutcome(str(getattr(outcome, "value", outcome))).value
        source = str(source_id).strip()
        if not source or len(source) > 500 or any(ord(ch) < 32 for ch in source):
            raise ValueError("source_id is invalid")
        con = self._begin()
        try:
            row = con.execute(
                "SELECT outcome,outcome_status FROM proactive_action WHERE action_id=? AND status='sent'",
                (action_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown sent proactive action: {action_id}")
            if row["outcome"] is not None:
                if row["outcome"] != resolved:
                    raise ValueError("proactive action already has a different outcome")
                self._finish(con)
                return {
                    "outcome": resolved,
                    "outcome_status": str(row["outcome_status"]),
                    "applied": False,
                }
            con.execute(
                """UPDATE proactive_action SET outcome=?,outcome_at=?,
                   outcome_status='provisional',outcome_source_id=?
                   WHERE action_id=? AND outcome IS NULL""",
                (resolved, timestamp, source, action_id),
            )
            self._finish(con)
            return {"outcome": resolved, "outcome_status": "provisional", "applied": True}
        except BaseException as exc:
            self._finish(con, exc)
            raise

    async def confirm_action_outcome(
        self,
        route: ContactRoute,
        action_id: str,
        *,
        inbound_text: str,
        model: str,
        extractor: Callable[..., Any],
        source_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = _finite(time.time() if now is None else now, "now")
        con = self._begin()
        try:
            action = con.execute(
                """SELECT outcome,outcome_at,outcome_status,contact_hash FROM proactive_action
                   WHERE action_id=? AND status='sent'""",
                (action_id,),
            ).fetchone()
            if action is None:
                raise KeyError(f"unknown sent proactive action: {action_id}")
            if action["contact_hash"] != route.contact_hash:
                raise ValueError("contact route does not own proactive action")
            existing = con.execute(
                "SELECT * FROM proactive_outcome_confirmation WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if existing is not None:
                self._finish(con)
                if existing["status"] == "confirmed":
                    self._ensure_outcome_projection(route, existing, now=timestamp)
                return self._confirmation_result(existing, applied=False)
            if action["outcome_status"] == "confirmed":
                self._finish(con)
                return {
                    "outcome": str(action["outcome"]),
                    "outcome_status": "confirmed",
                    "valence": _OUTCOME_VALENCE[str(action["outcome"])],
                    "applied": False,
                }
            if action["outcome"] is None or action["outcome_status"] != "provisional":
                raise ValueError("proactive action has no provisional outcome")
            pinned_model = str(model or "").strip()
            if (
                not pinned_model or len(pinned_model) > 200
                or any(ord(ch) < 32 for ch in pinned_model)
            ):
                raise ValueError("model is invalid")
            source = str(source_id).strip()
            if not source or len(source) > 500 or any(ord(ch) < 32 for ch in source):
                raise ValueError("source_id is invalid")
            bounded_text = str(inbound_text or "")[:_OUTCOME_TEXT_LIMIT]
            input_sha256 = hashlib.sha256(bounded_text.encode()).hexdigest()
            provisional = str(action["outcome"])
            con.execute(
                """INSERT INTO proactive_outcome_confirmation(
                   action_id,model,status,claimed_at,provisional_at,provisional_outcome,
                   input_sha256,input_chars,source_id
                   ) VALUES(?,?,'claimed',?,?,?,?,?,?)""",
                (
                    action_id, pinned_model, timestamp, float(action["outcome_at"]),
                    provisional, input_sha256, len(bounded_text), source,
                ),
            )
            self._finish(con)
        except BaseException as exc:
            if con.in_transaction:
                self._finish(con, exc)
            raise

        extracted: Any = None
        try:
            # One immediate retry repairs transient worker restarts/timeouts while
            # the bounded plaintext still exists only in this process. Malformed
            # model output is deterministic and is not retried.
            for attempt in range(2):
                try:
                    extracted = extractor(
                        model=pinned_model,
                        text=bounded_text,
                        provisional_outcome=provisional,
                    )
                    if inspect.isawaitable(extracted):
                        extracted = await extracted
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt:
                        raise
                    await asyncio.sleep(0)
            if not isinstance(extracted, Mapping):
                raise ValueError("extractor result must be an object")
            confirmed = ProactiveOutcome(str(extracted.get("outcome") or "")).value
            valence = str(extracted.get("valence") or "")
            if _OUTCOME_VALENCE[confirmed] != valence:
                raise ValueError("extractor valence does not match outcome")
        except asyncio.CancelledError:
            self._fail_outcome_confirmation(action_id, timestamp, "callback_cancelled")
            raise
        except Exception as exc:
            error_code = "malformed_result" if isinstance(exc, ValueError) else "callback_unavailable"
            audit = self._fail_outcome_confirmation(action_id, timestamp, error_code)
            return self._confirmation_result(audit, applied=False)

        con = self._begin()
        try:
            action = con.execute(
                "SELECT * FROM proactive_action WHERE action_id=?", (action_id,)
            ).fetchone()
            if action["outcome_status"] != "provisional":
                raise ValueError("confirmed proactive outcome cannot be rewritten")
            contact = con.execute(
                "SELECT negative_streak,disabled_until FROM proactive_contact WHERE contact_hash=?",
                (action["contact_hash"],),
            ).fetchone()
            streak = int(contact["negative_streak"])
            streak = streak + 1 if confirmed in _NEGATIVE_OUTCOMES else 0
            disabled_until = (
                timestamp + self.config.backoff_days * _DAY
                if streak >= self.config.backoff_after_dismissals and contact["disabled_until"] is None
                else contact["disabled_until"]
            )
            con.execute(
                """UPDATE proactive_action SET outcome=?,outcome_at=?,outcome_status='confirmed'
                   WHERE action_id=? AND outcome_status='provisional'""",
                (confirmed, timestamp, action_id),
            )
            con.execute(
                """UPDATE proactive_contact SET negative_streak=?,
                   disabled_until=CASE WHEN ? IS NULL THEN disabled_until ELSE ? END,updated_at=?
                   WHERE contact_hash=?""",
                (streak, disabled_until, disabled_until, timestamp, action["contact_hash"]),
            )
            con.execute(
                """UPDATE proactive_outcome_confirmation SET status='confirmed',completed_at=?,
                   confirmed_outcome=?,confirmed_valence=?,projection_state='pending',
                   projection_error=NULL WHERE action_id=? AND status='claimed'""",
                (timestamp, confirmed, valence, action_id),
            )
            audit = con.execute(
                "SELECT * FROM proactive_outcome_confirmation WHERE action_id=?", (action_id,)
            ).fetchone()
            self._finish(con)
        except BaseException as exc:
            self._finish(con, exc)
            raise
        self._ensure_outcome_projection(route, audit, now=timestamp)
        return self._confirmation_result(audit, applied=True)

    def _project_confirmed_outcome(
        self,
        route: ContactRoute,
        action_id: str,
        outcome: str,
        *,
        source_id: str,
        now: float,
    ) -> None:
        store = self._contact_store(route)
        if store.get_proactive_send(action_id) is not None:
            store.record_proactive_outcome(
                action_id, outcome, source_id=source_id, now=now,
            )

    def _ensure_outcome_projection(
        self, route: ContactRoute, audit: sqlite3.Row, *, now: float,
    ) -> None:
        if audit["projection_state"] == "complete":
            return
        action_id = str(audit["action_id"])
        try:
            self._project_confirmed_outcome(
                route,
                action_id,
                str(audit["confirmed_outcome"]),
                source_id=str(audit["source_id"]),
                now=now,
            )
        except Exception as exc:
            with self._connect() as con:
                con.execute(
                    """UPDATE proactive_outcome_confirmation
                       SET projection_state='failed',projection_error=?
                       WHERE action_id=? AND status='confirmed'""",
                    (type(exc).__name__, action_id),
                )
            raise
        with self._connect() as con:
            con.execute(
                """UPDATE proactive_outcome_confirmation
                   SET projection_state='complete',projection_error=NULL
                   WHERE action_id=? AND status='confirmed'""",
                (action_id,),
            )

    def _fail_outcome_confirmation(
        self, action_id: str, timestamp: float, error_code: str,
    ) -> sqlite3.Row:
        with self._connect() as failed:
            failed.execute(
                """UPDATE proactive_outcome_confirmation SET status='failed',completed_at=?,error_code=?
                   WHERE action_id=? AND status='claimed'""",
                (timestamp, error_code, action_id),
            )
            audit = failed.execute(
                "SELECT * FROM proactive_outcome_confirmation WHERE action_id=?", (action_id,)
            ).fetchone()
        if audit is None:
            raise RuntimeError("outcome confirmation audit disappeared")
        return audit

    @staticmethod
    def _confirmation_result(row: sqlite3.Row, *, applied: bool) -> dict[str, Any]:
        if row["status"] == "confirmed":
            return {
                "outcome": str(row["confirmed_outcome"]),
                "outcome_status": "confirmed",
                "valence": str(row["confirmed_valence"]),
                "applied": applied,
            }
        return {
            "outcome": str(row["provisional_outcome"]),
            "outcome_status": "provisional",
            "confirmation_status": str(row["status"]),
            "error_code": row["error_code"],
            "applied": False,
        }

    def expire_unanswered(self, *, now: float | None = None) -> list[str]:
        timestamp = _finite(time.time() if now is None else now, "now")
        with self._connect() as con:
            ids = [str(row[0]) for row in con.execute(
                """SELECT action_id FROM proactive_action WHERE status='sent' AND outcome IS NULL
                   AND sent_at<=?""",
                (timestamp - _DAY,),
            ).fetchall()]
        expired: list[str] = []
        for action_id in ids:
            try:
                self.set_action_outcome(action_id, ProactiveOutcome.IGNORED, now=timestamp)
                expired.append(action_id)
            except (KeyError, ValueError):
                continue
        return expired

    def record_outcome(
        self,
        route: ContactRoute,
        action_id: str,
        outcome: ProactiveOutcome | str,
        *,
        source_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Idempotently apply one outcome to state.db and its contact ledger."""
        timestamp = _finite(time.time() if now is None else now, "now")
        normalized = ProactiveOutcome(outcome)
        state_result = self.set_action_outcome(action_id, normalized, now=timestamp)
        store = self._contact_store(route)
        if store.get_proactive_send(action_id) is not None:
            store.record_proactive_outcome(
                action_id, normalized, source_id=source_id, now=timestamp,
            )
        return state_result

    def get_slot(self, slot_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM proactive_slot WHERE slot_id=?", (slot_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_contact(self, contact_hash: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM proactive_contact WHERE contact_hash=?", (contact_hash,)
            ).fetchone()
        return dict(row) if row is not None else None

    def _route_for_contact(self, contact: StateContact) -> ContactRoute:
        if not contact.contact_id:
            raise ValueError(f"proactive contact {contact.contact_key} has no canonical contact_id")
        return ContactRoute(
            contact_id=contact.contact_id,
            profile_name=contact.profile,
            timezone=contact.timezone_name,
            principal="guest" if contact.profile == "guest" else "owner",
            chat_type=str(contact.route.get("chat_type") or "dm"),
            chat_id=str(contact.route.get("chat_id") or ""),
            user_id=str(contact.route.get("user_id") or ""),
            session_id=str(contact.route.get("session_id") or ""),
        )

    def route_for_contact(self, contact: StateContact) -> ContactRoute:
        """Return the validated canonical route for a persisted contact."""
        return self._route_for_contact(contact)

    def _import_ledger_actions(self, route: ContactRoute) -> None:
        """Idempotently project pre-protocol sent ledger rows into state.db."""
        store = self._contact_store(route)
        sends = store.recent_proactive_sends(since=0, decision=GateDecision.SENT, limit=1000)
        if not sends:
            return
        pending_outcomes: list[tuple[str, ProactiveOutcome, float]] = []
        con = self._begin()
        try:
            contact = con.execute(
                "SELECT inbound_version FROM proactive_contact WHERE contact_hash=?",
                (route.contact_hash,),
            ).fetchone()
            for send in sorted(sends, key=lambda item: item.sent_at or item.created_at):
                if send.sent_at is None:
                    continue
                con.execute(
                    """INSERT OR IGNORE INTO proactive_action(
                       action_id,slot_id,contact_hash,interest_id,kind,status,sent_at,
                       inbound_version,reason,created_at)
                       VALUES(?,NULL,?,?,?,'sent',?,?,?,?)""",
                    (
                        send.send_id, route.contact_hash, send.interest_id, send.kind.value,
                        send.sent_at, int(contact["inbound_version"]), "ledger_import",
                        send.created_at,
                    ),
                )
                if send.outcome is not None:
                    pending_outcomes.append((
                        send.send_id, send.outcome,
                        float(send.outcome_at or send.sent_at or send.created_at),
                    ))
            self._finish(con)
        except BaseException as exc:
            self._finish(con, exc)
            raise
        for action_id, outcome, outcome_at in pending_outcomes:
            self.set_action_outcome(action_id, outcome, now=outcome_at)

    def _project_action_to_ledger(
        self, route: ContactRoute, claim: SlotClaim, *, now: float,
        gate_reason: str = "observe_mode",
    ) -> ProactiveSend:
        """Idempotent contact-ledger projection of the canonical state action."""
        store = self._contact_store(route)
        existing = store.get_proactive_send(claim.slot_id)
        if existing is not None:
            return existing
        if claim.kind == ProactiveSendKind.CHECKIN.value:
            raw_reason = str(claim.payload.get("reason") or "")
            candidate = {
                "audit": "checkin",
                "dry_run": True,
                "kind": str(claim.payload.get("kind") or "checkin")[:32],
                "reason_sha256": hashlib.sha256(raw_reason.encode("utf-8")).hexdigest(),
            }
        else:
            candidate = dict(claim.payload)
            candidate["dry_run"] = True
        return store.record_proactive_send(ProactiveSend(
            send_id=claim.slot_id,
            interest_id=claim.interest_id,
            kind=ProactiveSendKind(claim.kind),
            candidate_json=json.dumps(
                candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            gate_decision=GateDecision.SUPPRESSED,
            gate_reason=gate_reason,
            sent_at=None,
            outcome=None,
            outcome_at=None,
            created_at=now,
        ))

    def tick(
        self, *, now: float | None = None,
        on_dry_run: Callable[[ContactRoute, SlotClaim], Any] | None = None,
        on_interest_share: Callable[[ContactRoute, SlotClaim, ContactMemoryStore], Any] | None = None,
        on_prepared: Callable[[ContactRoute, SlotClaim, Any], None] | None = None,
    ) -> dict[str, int]:
        """Run the sole production policy engine; never calls a transport.

        ``on_interest_share`` is the Phase-4 isolated pipeline edge.  Its return
        value must expose ``status`` and ``reason``; only ``status='dry_run'``
        counts as a simulated fire.  Check-ins retain the Phase-3 flow unchanged.
        """
        result = {"armed": 0, "fired": 0, "ignored": 0}
        if not self.config.enabled:
            return result
        timestamp = _finite(time.time() if now is None else now, "now")
        self.record_health(
            "planning_attempt", {"state": "started", "tick_at": timestamp}, now=timestamp
        )
        rate_status = self.enforce_send_rate_circuit(now=timestamp)
        self.record_health("send_rate", rate_status, now=timestamp)
        if self.config.mode is ProactiveMode.LIVE and rate_status["circuit_opened"]:
            self.record_health(
                "planning_attempt", {"state": "blocked_by_send_rate", "tick_at": timestamp}, now=timestamp
            )
            return result
        self.cleanup_sprawl(now=timestamp)
        contacts = {item.contact_key: item for item in ProactiveStateStore(self.state_db_path).contacts()}
        routes: dict[str, ContactRoute] = {}
        for key, contact in contacts.items():
            route = self._route_for_contact(contact)
            routes[key] = route
            self._import_ledger_actions(route)

        expired = self.expire_unanswered(now=timestamp)
        for action_id in expired:
            with self._connect() as con:
                action = con.execute(
                    "SELECT contact_hash FROM proactive_action WHERE action_id=?", (action_id,)
                ).fetchone()
            route = routes.get(str(action["contact_hash"])) if action else None
            if route is not None:
                self.record_outcome(
                    route, action_id, ProactiveOutcome.IGNORED,
                    source_id=f"proactive-timeout:{action_id}", now=timestamp,
                )
            result["ignored"] += 1

        # Process old due work before planning; newly armed work cannot fire here.
        claims = self.claim_due(worker_id=f"tick-{uuid.uuid4().hex}", now=timestamp, limit=100)
        for claim in claims:
            route = routes.get(claim.contact_hash)
            if route is None:
                continue
            with self._connect() as con:
                retry = con.execute(
                    "SELECT prepared_payload,prepared_image_url FROM proactive_delivery "
                    "WHERE slot_id=? AND state='retry_wait'",
                    (claim.slot_id,),
                ).fetchone()
            if retry is not None:
                payload = str(retry["prepared_payload"] or "")
                if not payload:
                    self.finish_delivery(claim, state="failed", reason="retry_payload_missing", now=timestamp)
                elif on_prepared is not None:
                    on_prepared(route, claim, PreparedOutput(
                        payload,
                        optional_image_url=(
                            str(retry["prepared_image_url"])
                            if retry["prepared_image_url"] else None
                        ),
                        reason="immutable_retry_replay",
                    ))
                continue
            if claim.kind in {"interest_share", "exploration"} and on_interest_share is not None:
                store = self._contact_store(route)
                reused = self.reusable_cancelled_candidate(
                    route, topic=str(claim.payload.get("topic") or ""), now=timestamp,
                )
                if reused and "reused_candidate_json" not in claim.payload:
                    claim = replace(
                        claim,
                        payload={**claim.payload, "reused_candidate_json": reused},
                    )
                pipeline_result: Any = None
                try:
                    pipeline_result = on_interest_share(route, claim, store)
                    pipeline_status = str(
                        getattr(pipeline_result, "status", "")
                        or (pipeline_result.get("status") if isinstance(pipeline_result, Mapping) else "")
                    )
                    pipeline_reason = str(
                        getattr(pipeline_result, "reason", "")
                        or (pipeline_result.get("reason") if isinstance(pipeline_result, Mapping) else "")
                        or "pipeline_suppressed"
                    )
                except Exception:
                    logger.warning("Proactive interest pipeline failed", exc_info=True)
                    pipeline_status, pipeline_reason = "suppressed", "pipeline_error"
                    if store.get_proactive_send(claim.slot_id) is None:
                        store.record_proactive_send(ProactiveSend(
                            send_id=claim.slot_id, interest_id=claim.interest_id,
                            kind=ProactiveSendKind(claim.kind),
                            candidate_json=json.dumps({"error": pipeline_reason}, separators=(",", ":")),
                            gate_decision=GateDecision.SUPPRESSED, gate_reason=pipeline_reason,
                            sent_at=None, outcome=None, outcome_at=None, created_at=timestamp,
                        ))
                if not self._claim_is_current(claim):
                    candidate = getattr(pipeline_result, "candidate", None)
                    if candidate is not None:
                        self.cache_cancelled_candidate(claim, candidate, now=timestamp)
                    continue
                if pipeline_status == "prepared":
                    composed_text = str(getattr(pipeline_result, "composed_text", "") or "")
                    candidate = getattr(pipeline_result, "candidate", None)
                    image_url = getattr(candidate, "optional_image_url", None)
                    self.reserve_delivery(
                        claim, composed_text, image_url=image_url, now=timestamp,
                    )
                    if on_prepared is not None:
                        on_prepared(route, claim, pipeline_result)
                    continue
                status = self.complete_claim(
                    claim, sent=pipeline_status == "dry_run", reason=pipeline_reason, now=timestamp
                )
                if status == "dry_run":
                    result["fired"] += 1
                continue
            try:
                # Projection and child-turn initiation must both succeed before
                # the durable action/slot is marked fired.  A failure is re-armed
                # immediately rather than becoming an unretryable dry-run action.
                composed_text = ""
                if on_dry_run is not None:
                    initiation = on_dry_run(route, claim)
                    if isinstance(initiation, CheckinInitiationResult):
                        if not initiation.allowed:
                            self._project_action_to_ledger(
                                route,
                                claim,
                                now=timestamp,
                                gate_reason=initiation.reason,
                            )
                            self.complete_claim(
                                claim,
                                sent=False,
                                reason=initiation.reason,
                                now=timestamp,
                            )
                            continue
                        composed_text = initiation.text.strip()
                    else:
                        composed_text = str(initiation or "").strip()
                if self.config.mode is not ProactiveMode.LIVE:
                    self._project_action_to_ledger(route, claim, now=timestamp)
            except Exception:
                logger.warning("Proactive check-in initiation failed", exc_info=True)
                self.retry_claim(claim, reason="initiation_retry", now=timestamp)
                continue
            if self.config.mode is ProactiveMode.LIVE:
                if not composed_text:
                    self.complete_claim(claim, sent=False, reason="compose_unavailable", now=timestamp)
                    continue
                prepared = PreparedOutput(composed_text)
                self.reserve_delivery(claim, composed_text, now=timestamp)
                if on_prepared is not None:
                    on_prepared(route, claim, prepared)
                continue
            status = self.complete_claim(
                claim, sent=True, reason="observe_mode", now=timestamp
            )
            if status != "dry_run":
                continue
            result["fired"] += 1

        for contact in ProactiveStateStore(self.state_db_path).contacts():
            route = routes.get(contact.contact_key) or self._route_for_contact(contact)
            if self.get_contact(contact.contact_key) is None:
                continue
            with self._connect() as con:
                if con.execute(
                    "SELECT 1 FROM proactive_slot WHERE contact_hash=? AND status IN ('armed','claimed')",
                    (contact.contact_key,),
                ).fetchone() is not None:
                    continue
            try:
                if contact.pending_checkin_kind in {"serious", "open_loop"}:
                    checkin_kind = "serious" if contact.pending_checkin_kind == "serious" else "open_loop"
                    plan = plan_checkin(
                        contact_key=contact.contact_key, kind=checkin_kind,
                        last_user_ts=contact.last_inbound_at or timestamp,
                        reason=contact.pending_checkin_reason or "follow up",
                        timezone_name=contact.timezone_name,
                        active_start=self.config.active_start, active_end=self.config.active_end,
                    )
                    if plan is None:
                        continue
                    slot_id = self.arm_slot(
                        route, kind=ProactiveSendKind.CHECKIN, fire_at=plan.send_at_ts,
                        payload={
                            "kind": contact.pending_checkin_kind, "reason": plan.reason,
                            "session_id": route.session_id,
                        }, now=timestamp,
                    )
                    with self._connect() as con:
                        con.execute(
                            """UPDATE proactive_contact SET pending_checkin_kind=NULL,
                               pending_checkin_reason=NULL,updated_at=? WHERE contact_hash=?""",
                            (timestamp, contact.contact_key),
                        )
                else:
                    selection = self.select_topic(route, now=timestamp)
                    if selection is None:
                        continue
                    fire_at = push_into_active_hours(
                        timestamp + 3600,
                        timezone_name=contact.timezone_name,
                        active_start=self.config.active_start,
                        active_end=self.config.active_end,
                        jitter_key=f"{contact.contact_key}:interest",
                    )
                    slot_id = self.arm_slot(
                        route, kind=selection.kind, fire_at=fire_at,
                        interest_id=selection.interest.interest_id,
                        payload={
                            "topic": selection.interest.topic, "session_id": route.session_id,
                        }, now=timestamp,
                    )
                slot = self.get_slot(slot_id)
                if slot is not None and float(slot["created_at"]) == timestamp:
                    result["armed"] += 1
            except ValueError as exc:
                if "not eligible" not in str(exc):
                    raise
        self.record_health(
            "planning_attempt",
            {"state": "completed", "tick_at": timestamp, "armed": result["armed"]},
            now=timestamp,
        )
        return result


def assert_no_profile_conflicts(profile_contacts: Mapping[str, Sequence[str]]) -> None:
    owners: dict[str, str] = {}
    for profile, contacts in profile_contacts.items():
        for contact_id in contacts:
            key = Path(opaque_contact_filename(contact_id)).stem
            prior = owners.setdefault(key, str(profile))
            if prior != str(profile):
                raise RuntimeError(f"contact {contact_id!r} is owned by both {prior} and {profile}")


def classify_inbound_outcome(send: ProactiveSend, text: str) -> str:
    try:
        candidate = json.loads(send.candidate_json)
    except (TypeError, json.JSONDecodeError):
        candidate = {}
    topic = str(candidate.get("topic") or "") if isinstance(candidate, dict) else ""
    return ProactiveScheduler.classify_outcome(text, topic).value


def re_search_dismissive(text: str) -> bool:
    tokens = re.findall(r"[a-z]+(?:'[a-z]+)?", text.casefold())
    if tokens in (["ok"], ["okay"]):
        return False
    dismissive = {"k", "meh", "nah", "nope", "stop"}
    return bool(
        tokens
        and (
            any(token in dismissive for token in tokens)
            or any(
                tokens[index:index + 2] in (["don't", "care"], ["dont", "care"])
                for index in range(len(tokens) - 1)
            )
        )
    )


async def handle_inbound_async(
    *,
    profile_home: str | Path,
    profile_name: str,
    proactive_config: Mapping[str, Any],
    route: ContactRoute,
    message_id: str,
    text: str,
    received_at: float,
    serious: bool = False,
) -> dict[str, Any]:
    """Non-blocking gateway hook over the canonical synchronous protocol."""
    return await asyncio.to_thread(
        handle_inbound,
        state_db=Path(profile_home) / "state.db",
        contact_memory_root=Path(profile_home) / "contact-memory",
        profile=profile_name,
        contact_id=route.contact_id,
        route=route.as_dict(),
        timezone_name=route.timezone,
        source_id=message_id,
        text=text,
        received_at=received_at,
        config=ProactiveConfig.from_mapping(proactive_config),
        serious_register=serious,
    )


def handle_inbound(
    *,
    state_db: str | Path,
    contact_memory_root: str | Path,
    profile: str,
    contact_id: str,
    route: Mapping[str, Any],
    timezone_name: str,
    source_id: str,
    text: str,
    received_at: float,
    config: ProactiveConfig,
    serious_register: bool = False,
) -> dict[str, Any]:
    """Synchronous worker used by the gateway's ``asyncio.to_thread`` hook.

    The explicit paths are cross-checked rather than inferred from process-wide
    ``HERMES_HOME``.  This is load-bearing for multiplexed Poke/Guest routing.
    """
    profile_home = Path(state_db).expanduser().resolve().parent
    memory_root = Path(contact_memory_root).expanduser().resolve()
    if memory_root != (profile_home / "contact-memory").resolve():
        raise ValueError("contact-memory root does not match explicit profile state.db")
    serious = bool(serious_register)
    if not serious:
        try:
            from gateway.conversation_texture_v2 import _tier_of
            serious = bool(_tier_of(text or ""))
        except Exception:
            serious = False
    from gateway.proactive_checkin import detect_checkin_kind
    checkin_kind = detect_checkin_kind(text, serious_tier=1 if serious else 0)
    contact_route = ContactRoute(
        contact_id=contact_id,
        profile_name=profile,
        timezone=timezone_name or config.timezone,
        principal="guest" if profile == "guest" else "owner",
        chat_type=str(route.get("chat_type") or "dm"),
        chat_id=str(route.get("chat_id") or ""),
        user_id=str(route.get("user_id") or ""),
        session_id=str(route.get("session_id") or ""),
    )
    scheduler = ProactiveScheduler(
        state_db_path=state_db,
        profile_home=profile_home,
        profile_name=profile,
        contact_memory_root=memory_root,
        config=config,
    )
    result = scheduler.note_inbound(
        contact_route,
        message_id=source_id,
        received_at=received_at,
        serious=serious,
        checkin_kind=checkin_kind,
        checkin_reason=(text or "")[:500] if checkin_kind else None,
    )
    pending = result.get("pending_action")
    if pending:
        store = scheduler._contact_store(contact_route)
        ledger_send = store.get_proactive_send(str(pending["action_id"]))
        topic = ""
        if ledger_send is not None:
            outcome = ProactiveOutcome(classify_inbound_outcome(ledger_send, text))
        else:
            if pending.get("interest_id"):
                interest = store.get_interest(str(pending["interest_id"]))
                topic = interest.topic if interest else ""
            outcome = scheduler.classify_outcome(text, topic)
        provisional = scheduler.record_provisional_outcome(
            str(pending["action_id"]),
            outcome,
            source_id=f"inbound:{source_id}",
            now=received_at,
        )
        result["outcome"] = outcome.value
        result["outcome_status"] = provisional["outcome_status"]
    else:
        result["outcome"] = None
        result["outcome_status"] = None
    result["serious"] = serious
    return result


__all__ = [
    "ContactRoute", "ProactiveConfig", "ProactiveScheduler", "ProactiveStateStore",
    "SlotClaim", "StateContact", "TopicSelection", "assert_no_profile_conflicts",
    "assert_unique_contact_ownership", "classify_inbound_outcome", "handle_inbound",
    "handle_inbound_async",
]
