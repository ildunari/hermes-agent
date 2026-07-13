"""Persistent proactive scheduler for contact-isolated gateway profiles.

The scheduler owns *when*.  Interest-share claims are deliberately dry-run-only
in Phase 3: they are durably logged as suppressions and can never call a sender.
Check-ins share the same caps/one-strike/claim machinery but delivery remains an
explicit caller action through the assistant-first child-session path.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

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
from gateway.proactive_checkin import plan_checkin, push_into_active_hours

_WEEK = 7 * 86_400.0
_DAY = 86_400.0
_DIRECT_TYPES = frozenset({"dm", "direct", "private"})
_NEGATIVE_OUTCOMES = frozenset({"ignored", "dismissed"})

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
CREATE TABLE IF NOT EXISTS proactive_slot (
  slot_id TEXT PRIMARY KEY,
  contact_hash TEXT NOT NULL REFERENCES proactive_contact(contact_hash) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('interest_share','checkin','exploration')),
  interest_id TEXT,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('armed','claimed','fired','cancelled','suppressed')),
  fire_at REAL NOT NULL,
  inbound_version INTEGER NOT NULL,
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
  inbound_version INTEGER NOT NULL,
  reason TEXT NOT NULL,
  created_at REAL NOT NULL,
  CHECK((status='sent' AND sent_at IS NOT NULL) OR (status!='sent' AND sent_at IS NULL)),
  CHECK((outcome IS NULL AND outcome_at IS NULL) OR
        (outcome IS NOT NULL AND outcome_at IS NOT NULL AND sent_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS proactive_action_recent
  ON proactive_action(contact_hash, sent_at DESC, created_at DESC);
"""

_OWNERSHIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_contact_owner (
  contact_hash TEXT PRIMARY KEY,
  profile_name TEXT NOT NULL,
  state_db TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""


def _initialize_schema(con: sqlite3.Connection) -> None:
    """Install the additive schema and repair pre-index duplicate live slots."""
    con.executescript(_SCHEMA)
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
    return root / "proactive-contact-ownership.db"


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
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()


@dataclass(frozen=True)
class ProactiveConfig:
    enabled: bool = False
    dry_run: bool = True
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

    def validate(self) -> None:
        if self.min_gap_hours < 48:
            raise ValueError("min_gap_hours cannot be below 48")
        if self.weekly_interest_cap > self.weekly_total_cap:
            raise ValueError("weekly_interest_cap cannot exceed weekly_total_cap")
        if not (0.0 <= self.exploration_floor <= 1.0):
            raise ValueError("exploration_floor must be in [0, 1]")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ProactiveConfig":
        raw = raw or {}
        if isinstance(raw.get("agent"), Mapping):
            nested = raw.get("agent", {}).get("proactive", {})
            raw = nested if isinstance(nested, Mapping) else {}
        active = raw.get("active_hours") if isinstance(raw.get("active_hours"), Mapping) else {}
        config = cls(
            enabled=bool(raw.get("enabled", False)),
            # Phase 3 is structurally dry-run for interest shares even if a
            # caller hands us a future-looking false value.
            dry_run=True,
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


@dataclass(frozen=True)
class TopicSelection:
    interest: Interest
    kind: ProactiveSendKind
    exploration_probability: float


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
                   status,fire_at,inbound_version,created_at,updated_at)
                   VALUES(?,?,?,?,?,'armed',?,?,?,?)""",
                (slot_id, contact_key, kind, interest_id,
                 json.dumps(dict(payload), sort_keys=True), fire_at,
                 int(contact["inbound_version"]), planned_at, planned_at),
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
                   inbound_version,created_at,updated_at)
                   VALUES(?,?,?,?,?,'armed',?,?,?,?)""",
                (
                    identifier, contact_hash, resolved_kind, interest_id,
                    json.dumps(dict(payload or {}), sort_keys=True, ensure_ascii=False),
                    fire, int(contact["inbound_version"]), timestamp, timestamp,
                ),
            )
            self._finish(con)
        except BaseException as exc:
            self._finish(con, exc)
            raise
        return identifier

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
                        int(row["inbound_version"]), token,
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
        """Complete a lease; interest shares are forcibly converted to dry-run."""
        timestamp = _finite(time.time() if now is None else now, "now")
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
            status = "dry_run" if self.config.dry_run and sent else ("sent" if sent else "suppressed")
            slot_status = "fired" if status in {"sent", "dry_run"} else "suppressed"
            action_id = claim.slot_id
            con.execute(
                """INSERT OR IGNORE INTO proactive_action(
                   action_id,slot_id,contact_hash,interest_id,kind,status,sent_at,
                   inbound_version,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    action_id, claim.slot_id, row["contact_hash"], row["interest_id"], row["kind"],
                    status, timestamp if status == "sent" else None,
                    int(row["inbound_version"]),
                    "phase3_dry_run" if status == "dry_run" else str(reason), timestamp,
                ),
            )
            con.execute(
                """UPDATE proactive_slot SET status=?,reason=?,claim_token=NULL,claim_until=NULL,
                   updated_at=? WHERE slot_id=?""",
                (slot_status, "phase3_dry_run" if status == "dry_run" else str(reason), timestamp, claim.slot_id),
            )
            self._finish(con)
            return status
        except BaseException as exc:
            if con.in_transaction:
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
                "UPDATE proactive_action SET outcome=?,outcome_at=? WHERE action_id=? AND outcome IS NULL",
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
        self, route: ContactRoute, claim: SlotClaim, *, now: float
    ) -> ProactiveSend:
        """Idempotent contact-ledger projection of the canonical state action."""
        store = self._contact_store(route)
        existing = store.get_proactive_send(claim.slot_id)
        if existing is not None:
            return existing
        candidate = dict(claim.payload)
        candidate["dry_run"] = True
        return store.record_proactive_send(ProactiveSend(
            send_id=claim.slot_id,
            interest_id=claim.interest_id,
            kind=ProactiveSendKind(claim.kind),
            candidate_json=json.dumps(candidate, ensure_ascii=False, sort_keys=True),
            gate_decision=GateDecision.SUPPRESSED,
            gate_reason="phase3_dry_run",
            sent_at=None,
            outcome=None,
            outcome_at=None,
            created_at=now,
        ))

    def tick(
        self, *, now: float | None = None,
        on_dry_run: Callable[[ContactRoute, SlotClaim], None] | None = None,
    ) -> dict[str, int]:
        """Run the sole production policy engine; never calls a transport."""
        result = {"armed": 0, "fired": 0, "ignored": 0}
        if not self.config.enabled:
            return result
        timestamp = _finite(time.time() if now is None else now, "now")
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
            status = self.complete_claim(
                claim, sent=True, reason="phase3_dry_run", now=timestamp
            )
            if status != "dry_run":
                continue
            self._project_action_to_ledger(route, claim, now=timestamp)
            if on_dry_run is not None:
                on_dry_run(route, claim)
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
                if contact.pending_checkin_kind:
                    plan = plan_checkin(
                        contact_key=contact.contact_key, kind=contact.pending_checkin_kind,
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
    import re
    return bool(re.search(r"^(?:k|ok|okay|meh|nah|nope|stop|don't care|dont care)[.! ]*$", text))


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
    del serious
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
) -> dict[str, Any]:
    """Synchronous worker used by the gateway's ``asyncio.to_thread`` hook.

    The explicit paths are cross-checked rather than inferred from process-wide
    ``HERMES_HOME``.  This is load-bearing for multiplexed Poke/Guest routing.
    """
    profile_home = Path(state_db).expanduser().resolve().parent
    memory_root = Path(contact_memory_root).expanduser().resolve()
    if memory_root != (profile_home / "contact-memory").resolve():
        raise ValueError("contact-memory root does not match explicit profile state.db")
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
        scheduler.record_outcome(
            contact_route,
            str(pending["action_id"]),
            outcome,
            source_id=f"inbound:{source_id}",
            now=received_at,
        )
        result["outcome"] = outcome.value
    else:
        result["outcome"] = None
    result["serious"] = serious
    return result


__all__ = [
    "ContactRoute", "ProactiveConfig", "ProactiveScheduler", "ProactiveStateStore",
    "SlotClaim", "StateContact", "TopicSelection", "assert_no_profile_conflicts",
    "assert_unique_contact_ownership", "classify_inbound_outcome", "handle_inbound",
    "handle_inbound_async",
]
