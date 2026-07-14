"""SQLite repository with physical contact isolation and exact vector search."""

from __future__ import annotations

from contextlib import contextmanager
from array import array
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import time
import math
import re
from typing import Any, Iterable, Iterator, Sequence
import uuid

try:  # Optional acceleration; base Hermes does not require NumPy.
    import numpy as np  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    np = None

from .schema import (
    CONTACT_SCHEMA_SQL,
    SCHEMA_VERSION,
    INTEREST_HALF_LIFE_CLASSES,
    INTEREST_MAX_LIVE_TOPICS,
    AssertionType,
    Audience,
    CommunicationActorRole,
    CommunicationAttachment,
    CommunicationBundle,
    CommunicationDirection,
    CommunicationEnrichmentState,
    CommunicationEvent,
    CommunicationIngestResult,
    CommunicationKind,
    CommunicationLifecycle,
    CommunicationPrivacy,
    CommunicationReactionSubtype,
    CommunicationRecommendationEvent,
    CommunicationRecommendationOutcome,
    CommunicationRelation,
    CommunicationRelationType,
    CommunicationUrl,
    EntityMention,
    FactProposal,
    FactRecord,
    FactStatus,
    GateDecision,
    Interest,
    InterestEvent,
    InterestState,
    InterestValence,
    MentionPolicy,
    ProactiveOutcome,
    ProactiveSend,
    ProactiveSendKind,
    RetrievalPrincipal,
    SearchResult,
    SignalType,
    normalized_proactive_item_hash,
)
from .security import visibility_sql


_TOPIC_WORD_RE = re.compile(r"^[^\W_]+(?:['-][^\W_]+)*$", re.UNICODE)
_TOPIC_INSTRUCTION_RE = re.compile(
    r"(?:^ignore\s+(?:all\s+|the\s+)?(?:previous|prior)\b|"
    r"\b(?:system prompt|developer message|reveal secrets?)\b)",
    re.IGNORECASE,
)
_TOPIC_STOPWORDS = frozenset({
    "a", "an", "and", "are", "at", "for", "from", "i", "in", "is", "it",
    "my", "of", "on", "or", "that", "the", "their", "them", "they", "this",
    "to", "we", "with", "you", "your",
})
_TOPIC_JUNK = frozenset({"anything", "misc", "other", "something", "stuff", "thing", "things", "topic"})
_SENSITIVE_INTEREST_RE = re.compile(
    r"\b(?:abortion|abuse|addiction|affair|aids|allerg\w*|anxiety|api[ -]?key|"
    r"arrest|assault|bank\w*|bereavement|break[ -]?up|cancer|contraception|court|custody|"
    r"credential\w*|credit(?: card)?|crime|death|debt|depress\w*|diabet\w*|"
    r"diagnos\w*|disease\w*|disorder\w*|divorc\w*|domestic|doctor|dying|estate|"
    r"fertility|fetish\w*|financ\w*|funeral|grief|harm|health\w*|hiv|hospital|"
    r"illness|income|immigration|investment\w*|kink\w*|lawsuit|lawyer|legal|"
    r"loan\w*|login|marriage|medical|medication\w*|mental|money|mortgage|opioid|"
    r"overdose|passcode|passport|password|pin|porn\w*|pregnan\w*|prescription\w*|probation|"
    r"private key|rape|rehab|relationship\w*|salary|secret|seed phrase|sex\w*|"
    r"social security|spouse|ssn|std|sti|suicid\w*|surgery|tax\w*|therapy|token|"
    r"trauma|treatment|violence|visa)\b",
    re.IGNORECASE,
)
_MESSAGE_LENGTH_META_KEY = "contact_message_lengths_v1"
_MESSAGE_LENGTH_WINDOW = 50


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


def _finite_timestamp(value: float | None, *, name: str = "timestamp") -> float:
    """Resolve a timestamp and reject values SQLite cannot preserve faithfully."""
    timestamp = float(time.time() if value is None else value)
    if not math.isfinite(timestamp):
        raise ValueError(f"{name} must be finite")
    return timestamp


def _provided_timestamp(value: float | None, *, name: str = "timestamp") -> float | None:
    """Validate a caller-supplied time before acquiring a write transaction."""
    return None if value is None else _finite_timestamp(value, name=name)


def normalize_interest_topic(topic: object) -> str:
    """Return the canonical topic or reject text that is not a short noun phrase."""
    value = " ".join(str(topic or "").casefold().split())
    words = value.split()
    if not 1 <= len(words) <= 4 or len(value) > 80:
        raise ValueError("interest topic must contain one to four words")
    if any(not _TOPIC_WORD_RE.fullmatch(word) for word in words):
        raise ValueError("interest topic contains invalid characters")
    if not any(character.isalpha() for character in value):
        raise ValueError("interest topic must contain letters")
    if _TOPIC_INSTRUCTION_RE.search(value):
        raise ValueError("instruction-like text is not an interest topic")
    if value in _TOPIC_JUNK or all(word in _TOPIC_STOPWORDS for word in words):
        raise ValueError("interest topic is too generic")
    if _SENSITIVE_INTEREST_RE.search(value):
        raise ValueError("sensitive text cannot be stored as an interest topic")
    return value


@dataclass(frozen=True)
class MessageLengthObservation:
    message_length: int
    baseline_median: float | None
    is_long_reply: bool
    sample_count: int


@dataclass(frozen=True)
class MessageLengthBaseline:
    sample_count: int
    median: float | None


def opaque_contact_filename(contact_id: str) -> str:
    if not str(contact_id).strip():
        raise ValueError("contact_id is required")
    digest = hashlib.sha256(("hermes-contact-memory-v1\0" + str(contact_id)).encode()).hexdigest()
    return f"{digest}.sqlite3"


class ContactMemoryStore:
    def __init__(self, root: str | Path, contact_id: str, *, timeout: float = 10.0):
        self.root = Path(root)
        self.contact_id = str(contact_id)
        self.path = self.root / "contacts" / opaque_contact_filename(self.contact_id)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.timeout = timeout
        self._initialize()
        try:
            self.path.parent.chmod(0o700)
            self.path.chmod(0o600)
        except OSError:
            pass

    @property
    def contact_namespace(self) -> str:
        """Opaque filename stem shared by this contact's DB and digest."""
        return self.path.stem

    @classmethod
    def open_existing(cls, root: str | Path, path: str | Path, *, timeout: float = 10.0):
        """Open an enumerated opaque contact DB without recovering its contact ID.

        Contact identifiers are deliberately absent from filenames and ledger
        rows.  Offline profile maintenance only needs the already-routed DB and
        its opaque namespace, so it must not invent or persist a reverse map.
        """
        root_path = Path(root).expanduser().resolve()
        candidate = Path(path).expanduser().resolve()
        contacts = (root_path / "contacts").resolve()
        if candidate.parent != contacts or not re.fullmatch(r"[0-9a-f]{64}\.sqlite3", candidate.name):
            raise ValueError("contact database path is outside the opaque contacts directory")
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        store = cls.__new__(cls)
        store.root = root_path
        store.contact_id = ""
        store.path = candidate
        store.timeout = timeout
        store._initialize()
        return store

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA secure_delete=ON")
        con.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        return con

    def _initialize(self) -> None:
        with self._connect() as con:
            existing = con.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone() if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone() else None
            supported = {"1", "2", "3", "4", "5", str(SCHEMA_VERSION)}
            if existing is not None and str(existing[0]) not in supported:
                raise RuntimeError(
                    f"unsupported contact-memory schema {existing[0]}; expected {SCHEMA_VERSION}"
                )
            if existing is not None and str(existing[0]) != str(SCHEMA_VERSION):
                self._upgrade_to_current(con)
                return
            con.executescript(CONTACT_SCHEMA_SQL)
            con.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _upgrade_to_current(self, con: sqlite3.Connection) -> None:
        """Atomically migrate a supported historical contact database to current."""
        con.execute("BEGIN IMMEDIATE")
        try:
            current = con.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            from_version = str(current[0]) if current is not None else ""
            if from_version == str(SCHEMA_VERSION):
                con.execute("COMMIT")
                return
            if from_version not in {"1", "2", "3", "4", "5"}:
                raise RuntimeError(
                    f"unsupported contact-memory schema {from_version}; expected {SCHEMA_VERSION}"
                )
            if from_version == "1":
                self._migrate_v1_to_v2(con)
            self._migrate_proactive_item_hash(con)
            self._migrate_communication_reaction_subtype(con)
            self._execute_script(con, CONTACT_SCHEMA_SQL)
            con.execute(
                "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise

    @staticmethod
    def _execute_script(con: sqlite3.Connection, script: str) -> None:
        """Execute a SQL script without ``executescript``'s implicit commit."""
        statement = ""
        for character in script:
            statement += character
            if character == ";" and sqlite3.complete_statement(statement):
                con.execute(statement)
                statement = ""
        if statement.strip():
            raise ValueError("incomplete SQL statement")

    @staticmethod
    def _migrate_v1_to_v2(con: sqlite3.Connection) -> None:
        """Upgrade mutable ledgers while leaving immutable fact history intact."""
        ContactMemoryStore._execute_script(con, """
        ALTER TABLE pending_fact RENAME TO pending_fact_v1;
        CREATE TABLE pending_fact (
          proposal_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
          payload_json TEXT NOT NULL, source_id TEXT NOT NULL,
          source_contact_id TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','superseded','promoted')),
          created_at REAL NOT NULL, decided_at REAL
        );
        INSERT INTO pending_fact SELECT * FROM pending_fact_v1;
        DROP TABLE pending_fact_v1;
        ALTER TABLE recommendation ADD COLUMN updated_at REAL;
        ALTER TABLE recommendation ADD COLUMN expires_at REAL;
        ALTER TABLE recommendation ADD COLUMN change_requirements_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE recommendation ADD COLUMN idempotency_key TEXT;
        CREATE UNIQUE INDEX recommendation_idempotency
          ON recommendation(idempotency_key) WHERE idempotency_key IS NOT NULL;
        CREATE TABLE callback_event (
          event_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
          subject_type TEXT NOT NULL CHECK(subject_type IN ('fact','recommendation')),
          subject_id TEXT NOT NULL, turn_index INTEGER NOT NULL, created_at REAL NOT NULL,
          UNIQUE(session_key, subject_type, subject_id, turn_index)
        );
        CREATE INDEX callback_event_cooldown
          ON callback_event(session_key, subject_type, subject_id, created_at, turn_index);
        """)

    @staticmethod
    def _migrate_proactive_item_hash(con: sqlite3.Connection) -> None:
        """Add and backfill the all-history novelty key without rebuilding the ledger."""
        table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proactive_send'"
        ).fetchone()
        if table is None:
            return
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(proactive_send)")}
        if "item_hash" not in columns:
            con.execute("ALTER TABLE proactive_send ADD COLUMN item_hash TEXT")
        rows = con.execute(
            "SELECT send_id,candidate_json FROM proactive_send WHERE item_hash IS NULL"
        )
        for row in rows:
            item_hash = ContactMemoryStore._candidate_item_hash(str(row["candidate_json"]))
            if item_hash is not None:
                con.execute(
                    "UPDATE proactive_send SET item_hash=? WHERE send_id=?",
                    (item_hash, row["send_id"]),
                )
        con.execute(
            "CREATE INDEX IF NOT EXISTS proactive_send_item_hash "
            "ON proactive_send(item_hash) WHERE item_hash IS NOT NULL"
        )

    @staticmethod
    def _migrate_communication_reaction_subtype(con: sqlite3.Connection) -> None:
        """Mark v5 reactions as historical rather than inventing a concrete subtype."""
        table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='communication_event'"
        ).fetchone()
        if table is None:
            return
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(communication_event)")}
        if "reaction_subtype" in columns:
            return
        con.execute(
            "ALTER TABLE communication_event ADD COLUMN reaction_subtype TEXT "
            "CHECK(reaction_subtype IN "
            "('like','love','dislike','laugh','emphasis','question','legacy_untyped'))"
        )
        con.execute(
            "UPDATE communication_event SET reaction_subtype='legacy_untyped' "
            "WHERE kind IN ('reaction_add','reaction_remove')"
        )

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    @contextmanager
    def interest_maintenance_transaction(self) -> Iterator[sqlite3.Connection]:
        """Expose one writer transaction to the offline maintenance engine.

        Phase-2 maintenance deliberately spans folding, taxonomy changes,
        lifecycle transitions, cap enforcement, and its bookkeeping row.  Those
        operations must share one ``BEGIN IMMEDIATE`` rather than nesting the
        ordinary one-operation store methods (which would commit partial work).
        The connection is intentionally scoped to the context and rolls back on
        *any* exception.
        """
        with self._immediate() as con:
            yield con

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> FactRecord:
        return FactRecord(
            logical_id=row["logical_id"], subject_id=row["subject_id"],
            predicate=row["predicate"], object_text=row["object_text"],
            audience=Audience(row["audience"]), mention_policy=MentionPolicy(row["mention_policy"]),
            assertion_type=AssertionType(row["assertion_type"]), source_id=row["source_id"],
            source_contact_id=row["source_contact_id"], evidence_pointer=row["evidence_pointer"],
            trust=float(row["trust"]), confidence=float(row["confidence"]),
            status=FactStatus(row["status"]), valid_from=float(row["valid_from"]),
            valid_to=row["valid_to"], metadata=json.loads(row["metadata_json"] or "{}"),
            version_id=row["version_id"], tx_from=float(row["tx_from"]),
            tx_to=row["tx_to"], created_at=float(row["created_at"]),
        )

    @staticmethod
    def _insert_fact(
        con: sqlite3.Connection,
        proposal: FactProposal,
        *,
        timestamp: float,
        supersede_active: bool = True,
    ) -> FactRecord:
        """Insert one immutable version inside an existing writer transaction.

        A non-active proposal is review state, not a newer truth.  It must never
        close an already reviewed active version with the same logical ID.
        """
        active = con.execute(
            "SELECT valid_from,tx_from FROM fact "
            "WHERE logical_id=? AND status='active' AND tx_to IS NULL",
            (proposal.logical_id,),
        ).fetchone()
        latest_tx = con.execute(
            "SELECT max(tx_from) FROM fact WHERE logical_id=?", (proposal.logical_id,)
        ).fetchone()[0]
        if latest_tx is not None and timestamp <= float(latest_tx):
            timestamp = math.nextafter(float(latest_tx), math.inf)
        valid_from = float(proposal.valid_from if proposal.valid_from is not None else timestamp)
        if active is not None and proposal.status is FactStatus.ACTIVE and supersede_active:
            # SQLite requires valid_to > valid_from.  Equality can occur in
            # deterministic tests and coarse clocks, so advance by one float ULP.
            if valid_from <= float(active["valid_from"]):
                valid_from = math.nextafter(float(active["valid_from"]), math.inf)
            con.execute(
                "UPDATE fact SET status='superseded',valid_to=?,tx_to=? "
                "WHERE logical_id=? AND status='active' AND tx_to IS NULL",
                (valid_from, timestamp, proposal.logical_id),
            )
        version_id = uuid.uuid4().hex
        con.execute(
            """INSERT INTO fact(
              version_id,logical_id,subject_id,predicate,object_text,audience,mention_policy,
              assertion_type,source_id,source_contact_id,evidence_pointer,trust,confidence,status,
              valid_from,valid_to,tx_from,tx_to,created_at,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id, proposal.logical_id, proposal.subject_id, proposal.predicate,
                proposal.object_text, proposal.audience.value, proposal.mention_policy.value,
                proposal.assertion_type.value, proposal.source_id, proposal.source_contact_id,
                proposal.evidence_pointer, proposal.trust, proposal.confidence, proposal.status.value,
                valid_from, proposal.valid_to, timestamp, None, timestamp,
                json.dumps(proposal.metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        return ContactMemoryStore._row_to_fact(
            con.execute("SELECT * FROM fact WHERE version_id=?", (version_id,)).fetchone()
        )

    def supersede_fact(self, proposal: FactProposal, *, now: float | None = None) -> FactRecord:
        if proposal.source_contact_id != self.contact_id:
            raise ValueError("source_contact_id does not match physical contact namespace")
        supplied_timestamp = _provided_timestamp(now)
        with self._immediate() as con:
            # Capture time after acquiring the writer lock. A timestamp sampled
            # before BEGIN IMMEDIATE can precede a faster competing writer's
            # tx_from and violate the bitemporal ordering invariant.
            timestamp = supplied_timestamp if supplied_timestamp is not None else _finite_timestamp(None)
            record = self._insert_fact(con, proposal, timestamp=timestamp)
        return record

    def revoke_fact(self, logical_id: str, *, now: float | None = None) -> bool:
        supplied_timestamp = _provided_timestamp(now)
        with self._immediate() as con:
            active = con.execute(
                "SELECT valid_from,tx_from FROM fact WHERE logical_id=? "
                "AND status='active' AND tx_to IS NULL", (logical_id,),
            ).fetchone()
            if active is None:
                return False
            timestamp = supplied_timestamp if supplied_timestamp is not None else _finite_timestamp(None)
            timestamp = max(
                timestamp,
                math.nextafter(float(active["valid_from"]), math.inf),
                math.nextafter(float(active["tx_from"]), math.inf),
            )
            cur = con.execute(
                "UPDATE fact SET status='withdrawn',valid_to=?, "
                "tx_to=? WHERE logical_id=? AND status='active' AND tx_to IS NULL",
                (timestamp, timestamp, logical_id),
            )
            return cur.rowcount > 0

    def import_proposals(self, proposals: Sequence[FactProposal], *, now: float | None = None) -> dict[str, int]:
        """Atomically import a dossier, idempotently keyed by source ID.

        Any validation or SQLite failure rolls back the entire dossier.  A
        quarantined/pending retry is stored as review evidence but cannot
        supersede an active reviewed fact.
        """
        if any(p.source_contact_id != self.contact_id for p in proposals):
            raise ValueError("source_contact_id does not match physical contact namespace")
        if len({p.source_id for p in proposals}) != len(proposals):
            raise ValueError("duplicate source IDs in import")
        supplied_timestamp = _provided_timestamp(now)
        inserted = skipped = 0
        with self._immediate() as con:
            timestamp = supplied_timestamp if supplied_timestamp is not None else _finite_timestamp(None)
            for proposal in proposals:
                exists = con.execute(
                    "SELECT 1 FROM fact WHERE source_contact_id=? AND source_id=?",
                    (self.contact_id, proposal.source_id),
                ).fetchone()
                if exists:
                    skipped += 1
                    continue
                self._insert_fact(con, proposal, timestamp=timestamp)
                inserted += 1
                timestamp = math.nextafter(timestamp, math.inf)
        return {"inserted": inserted, "skipped": skipped}

    def import_bootstrap_batch(
        self,
        proposals: Sequence[FactProposal],
        events: Sequence[InterestEvent],
        *,
        run_id: str,
        source_hash: str,
        manifest: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, int | bool]:
        """Atomically import facts, interest evidence, and the reviewed run marker."""
        run_id, source_hash = str(run_id).strip(), str(source_hash).strip()
        if not run_id or not source_hash:
            raise ValueError("run_id and source_hash are required")
        if any(p.source_contact_id != self.contact_id for p in proposals):
            raise ValueError("source_contact_id does not match physical contact namespace")
        fact_sources = [p.source_id for p in proposals]
        event_ids = [event.event_id for event in events]
        if len(fact_sources) != len(set(fact_sources)) or len(event_ids) != len(set(event_ids)):
            raise ValueError("duplicate IDs in bootstrap batch")
        timestamp = _finite_timestamp(now)
        inserted_facts = inserted_events = skipped_facts = skipped_events = 0
        with self._immediate() as con:
            prior = con.execute("SELECT source_hash FROM import_run WHERE run_id=?", (run_id,)).fetchone()
            same_source = con.execute("SELECT run_id FROM import_run WHERE source_hash=?", (source_hash,)).fetchone()
            if prior is not None or same_source is not None:
                if prior is not None and str(prior["source_hash"]) != source_hash:
                    raise ValueError("run_id already belongs to another source")
                return {"already_applied": True, "inserted_facts": 0, "inserted_events": 0,
                        "skipped_facts": len(proposals), "skipped_events": len(events)}
            fact_timestamp = timestamp
            for proposal in proposals:
                if con.execute("SELECT 1 FROM fact WHERE source_contact_id=? AND source_id=?",
                               (self.contact_id, proposal.source_id)).fetchone():
                    skipped_facts += 1
                else:
                    self._insert_fact(con, proposal, timestamp=fact_timestamp)
                    inserted_facts += 1
                    fact_timestamp = math.nextafter(fact_timestamp, math.inf)
            for event in events:
                changed = con.execute(
                    "INSERT OR IGNORE INTO interest_event(event_id,topic_text,signal_type,valence,source_id,created_at,folded_at) VALUES(?,?,?,?,?,?,?)",
                    (event.event_id, event.topic_text, event.signal_type.value, event.valence.value,
                     event.source_id, event.created_at, event.folded_at),
                ).rowcount
                inserted_events += int(changed)
                skipped_events += int(not changed)
            con.execute(
                "INSERT INTO import_run(run_id,source_hash,manifest_json,fact_count,interest_count,created_at) VALUES(?,?,?,?,?,?)",
                (run_id, source_hash, json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                 len(proposals), len(events), timestamp),
            )
        return {"already_applied": False, "inserted_facts": inserted_facts,
                "inserted_events": inserted_events, "skipped_facts": skipped_facts,
                "skipped_events": skipped_events}

    def import_reviewed_interest_seed(
        self,
        events: Sequence[InterestEvent],
        *,
        run_id: str,
        source_hash: str,
        manifest: dict[str, Any],
        seed_score: float = 3.0,
        half_life_days: float = 365.0,
        now: float | None = None,
    ) -> dict[str, object]:
        """Atomically import, fold, and activate one reviewed interest seed."""
        run_id, source_hash = str(run_id).strip(), str(source_hash).strip()
        if not run_id or not source_hash:
            raise ValueError("run_id and source_hash are required")
        if not events or len({event.event_id for event in events}) != len(events):
            raise ValueError("reviewed seed requires unique interest events")
        timestamp = _finite_timestamp(now)
        score = max(2.0, float(seed_score))
        half_life = max(1.0, float(half_life_days))
        activated: list[str] = []
        inserted_events = skipped_events = 0
        with self._immediate() as con:
            prior = con.execute("SELECT source_hash FROM import_run WHERE run_id=?", (run_id,)).fetchone()
            same_source = con.execute("SELECT run_id FROM import_run WHERE source_hash=?", (source_hash,)).fetchone()
            if prior is not None or same_source is not None:
                if prior is not None and str(prior["source_hash"]) != source_hash:
                    raise ValueError("run_id already belongs to another source")
                rows = con.execute(
                    "SELECT topic FROM interest WHERE state='active' AND retired_at IS NULL"
                ).fetchall()
                wanted = {normalize_interest_topic(event.topic_text) for event in events}
                return {"already_applied": True, "inserted_events": 0,
                        "skipped_events": len(events), "activated_topics": sorted(
                            str(row["topic"]) for row in rows if str(row["topic"]) in wanted
                        )}
            for event in events:
                topic = normalize_interest_topic(event.topic_text)
                existing_event = con.execute(
                    "SELECT topic_text,signal_type,valence,source_id,created_at FROM interest_event WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
                if existing_event is not None:
                    expected = (
                        topic, event.signal_type.value, event.valence.value,
                        event.source_id, float(event.created_at),
                    )
                    actual = (
                        str(existing_event["topic_text"]), str(existing_event["signal_type"]),
                        str(existing_event["valence"]), str(existing_event["source_id"]),
                        float(existing_event["created_at"]),
                    )
                    if actual != expected:
                        raise ValueError("reviewed seed event ID conflicts with stored evidence")
                    skipped_events += 1
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO interest_event(event_id,topic_text,signal_type,valence,source_id,created_at,folded_at) VALUES(?,?,?,?,?,?,?)",
                    (event.event_id, topic, event.signal_type.value, event.valence.value,
                     event.source_id, event.created_at, timestamp),
                )
                inserted_events += 1
                existing = con.execute(
                    "SELECT * FROM interest WHERE topic=? AND retired_at IS NULL", (topic,)
                ).fetchone()
                if existing is None:
                    con.execute(
                        """INSERT INTO interest(
                          interest_id,topic,parent_id,raw_score,last_evidence_at,evidence_count,
                          valence,half_life_days,state,ts_alpha,ts_beta,created_at,updated_at,retired_at
                        ) VALUES(?,?,NULL,?,?,?,?,?,'active',1.0,1.0,?,?,NULL)""",
                        (uuid.uuid4().hex, topic, score, timestamp, 1,
                         InterestValence.POSITIVE.value, half_life, timestamp, timestamp),
                    )
                else:
                    con.execute(
                        """UPDATE interest SET raw_score=?,last_evidence_at=?,evidence_count=?,
                           valence=?,half_life_days=?,state='active',updated_at=?,retired_at=NULL
                           WHERE interest_id=?""",
                        (max(score, float(existing["raw_score"])), timestamp,
                         int(existing["evidence_count"]) + 1, InterestValence.POSITIVE.value,
                         half_life, timestamp, str(existing["interest_id"])),
                    )
                activated.append(topic)
            con.execute(
                "INSERT INTO import_run(run_id,source_hash,manifest_json,fact_count,interest_count,created_at) VALUES(?,?,?,?,?,?)",
                (run_id, source_hash, json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                 0, len(events), timestamp),
            )
        return {"already_applied": False, "inserted_events": inserted_events,
                "skipped_events": skipped_events, "activated_topics": sorted(activated)}

    def active_facts(self, principal: RetrievalPrincipal, *, now: float | None = None) -> list[FactRecord]:
        timestamp = _finite_timestamp(now)
        clause, params = visibility_sql(principal, timestamp)
        with self._connect() as con:
            rows = con.execute(f"SELECT * FROM fact WHERE {clause} ORDER BY created_at DESC", params).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def count_versions(self, logical_id: str) -> int:
        with self._connect() as con:
            return int(con.execute("SELECT count(*) FROM fact WHERE logical_id=?", (logical_id,)).fetchone()[0])

    def put_embedding(self, version_id: str, model_id: str, vector: Sequence[float]) -> None:
        values = [float(value) for value in vector]
        if not values or not all(value == value and abs(value) != float("inf") for value in values):
            raise ValueError("embedding must be a finite, non-empty vector")
        norm = sum(value * value for value in values) ** 0.5
        if norm == 0:
            raise ValueError("embedding cannot be zero")
        arr = array("f", (value / norm for value in values))
        with self._immediate() as con:
            con.execute(
                "INSERT INTO embedding(version_id,model_id,dimensions,vector_le_f32,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(version_id,model_id) DO UPDATE SET dimensions=excluded.dimensions, "
                "vector_le_f32=excluded.vector_le_f32,created_at=excluded.created_at",
                (version_id, model_id, len(arr), arr.tobytes(), time.time()),
            )

    def embedding_coverage(
        self,
        principal: RetrievalPrincipal,
        model_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, object]:
        """Return text-free vector coverage diagnostics for authorized active facts."""
        timestamp = _finite_timestamp(now)
        clause, params = visibility_sql(principal, timestamp, alias="f")
        with self._connect() as con:
            active = int(con.execute(
                f"SELECT count(*) FROM fact f WHERE {clause}", params
            ).fetchone()[0])
            rows = con.execute(
                f"SELECT e.model_id,e.dimensions,count(*) AS n "
                f"FROM fact f JOIN embedding e ON e.version_id=f.version_id "
                f"WHERE {clause} GROUP BY e.model_id,e.dimensions",
                params,
            ).fetchall()
        matching = sum(int(row["n"]) for row in rows if row["model_id"] == model_id)
        dimensions = sorted({
            int(row["dimensions"]) for row in rows if row["model_id"] == model_id
        })
        return {
            "active_authorized_facts": active,
            "matching_vectors": matching,
            "matching_dimensions": dimensions,
            "complete": active > 0 and matching == active and len(dimensions) == 1,
        }

    def vector_search(self, principal: RetrievalPrincipal, query_vector: Sequence[float], *, model_id: str, limit: int = 3, now: float | None = None) -> list[SearchResult]:
        query_values = [float(value) for value in query_vector]
        norm = sum(value * value for value in query_values) ** 0.5
        if not query_values or norm != norm or norm == 0:
            return []
        query_values = [value / norm for value in query_values]
        timestamp = _finite_timestamp(now)
        clause, params = visibility_sql(principal, timestamp, alias="f")
        with self._connect() as con:
            rows = con.execute(
                f"SELECT f.*,e.dimensions,e.vector_le_f32 FROM fact f JOIN embedding e ON e.version_id=f.version_id "
                f"WHERE e.model_id=? AND {clause}", (model_id, *params),
            ).fetchall()
        compatible = [row for row in rows if int(row["dimensions"]) == len(query_values)]
        if not compatible:
            return []
        if np is not None:
            query = np.asarray(query_values, dtype=np.float32)
            matrix = np.ascontiguousarray(np.vstack([np.frombuffer(row["vector_le_f32"], dtype="<f4") for row in compatible]))
            scores = matrix @ query
            order = np.argsort(scores)[::-1][:max(0, min(int(limit), 20))]
            return [SearchResult(self._row_to_fact(compatible[int(i)]), float(scores[int(i)]), semantic_score=float(scores[int(i)])) for i in order]
        scored = []
        for row in compatible:
            values = array("f")
            values.frombytes(row["vector_le_f32"])
            score = sum(left * right for left, right in zip(values, query_values))
            scored.append(SearchResult(self._row_to_fact(row), score, semantic_score=score))
        scored.sort(key=lambda result: result.score, reverse=True)
        return scored[:max(0, min(int(limit), 20))]

    def lexical_search(self, principal: RetrievalPrincipal, query: str, *, limit: int = 3, now: float | None = None) -> list[SearchResult]:
        tokens = {token for token in "".join(ch.lower() if ch.isalnum() else " " for ch in query).split() if len(token) > 1}
        if not tokens:
            return []
        scored: list[SearchResult] = []
        for fact in self.active_facts(principal, now=now):
            haystack = " ".join((fact.subject_id, fact.predicate, fact.object_text)).lower()
            hay_tokens = set("".join(ch if ch.isalnum() else " " for ch in haystack).split())
            overlap = len(tokens & hay_tokens) / max(1, len(tokens))
            substring = sum(token in haystack for token in tokens) / max(1, len(tokens))
            score = 0.7 * overlap + 0.3 * substring
            if score:
                scored.append(SearchResult(fact, score, lexical_score=score))
        scored.sort(key=lambda result: (result.score, result.fact.trust, result.fact.confidence), reverse=True)
        return scored[:max(0, min(int(limit), 20))]

    def record_recall(self, session_key: str, turn_index: int, fact_ids: Iterable[str], event_type: str = "retrieved", *, now: float | None = None) -> None:
        if event_type not in {"retrieved", "used"}:
            raise ValueError("invalid recall event type")
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            for fact_id in dict.fromkeys(str(x) for x in fact_ids):
                con.execute(
                    "INSERT OR IGNORE INTO recall_event(event_id,session_key,turn_index,fact_version_id,event_type,created_at) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, session_key, int(turn_index), fact_id, event_type, timestamp),
                )

    def recently_retrieved(self, session_key: str, turn_index: int, *, turns: int = 10) -> set[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT fact_version_id FROM recall_event WHERE session_key=? AND event_type='retrieved' AND turn_index>=?",
                (session_key, max(0, int(turn_index) - int(turns))),
            ).fetchall()
        return {row[0] for row in rows}

    def ingest_extracted_fact(
        self,
        proposal: FactProposal,
        *,
        idempotency_key: str,
        auto_promote: bool = False,
        now: float | None = None,
    ) -> dict[str, object]:
        """Persist one validated extraction with atomic dedupe/supersession.

        Every extraction gets a ledger row. Promotion and closing the previous
        active version happen in the same writer transaction.
        """
        if proposal.source_contact_id != self.contact_id:
            raise ValueError("source_contact_id does not match physical contact namespace")
        payload = {
            **proposal.__dict__,
            "audience": proposal.audience.value,
            "mention_policy": proposal.mention_policy.value,
            "assertion_type": proposal.assertion_type.value,
            "status": proposal.status.value,
        }
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            existing = con.execute(
                "SELECT proposal_id,status FROM pending_fact WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return {"proposal_id": str(existing["proposal_id"]), "status": str(existing["status"]), "deduplicated": True}
            proposal_id = uuid.uuid4().hex
            # A correction makes older unresolved proposals for the same logical
            # slot stale; reviewers must never approve both later.
            rows = con.execute(
                "SELECT proposal_id,payload_json FROM pending_fact WHERE status='pending'"
            ).fetchall()
            for row in rows:
                old = json.loads(row["payload_json"])
                if old.get("logical_id") == proposal.logical_id and (
                    old.get("subject_id"), old.get("predicate"), old.get("object_text")
                ) != (proposal.subject_id, proposal.predicate, proposal.object_text):
                    con.execute(
                        "UPDATE pending_fact SET status='superseded',decided_at=? WHERE proposal_id=?",
                        (timestamp, row["proposal_id"]),
                    )
            state = "promoted" if auto_promote else "pending"
            con.execute(
                "INSERT INTO pending_fact(proposal_id,idempotency_key,payload_json,source_id,source_contact_id,status,created_at,decided_at) VALUES(?,?,?,?,?,?,?,?)",
                (proposal_id, idempotency_key, json.dumps(payload, ensure_ascii=False, sort_keys=True), proposal.source_id, proposal.source_contact_id, state, timestamp, timestamp if auto_promote else None),
            )
            record = None
            if auto_promote:
                active = FactProposal(
                    logical_id=proposal.logical_id, subject_id=proposal.subject_id,
                    predicate=proposal.predicate, object_text=proposal.object_text,
                    audience=Audience.OWNER_ONLY, mention_policy=MentionPolicy.BACKGROUND,
                    assertion_type=AssertionType.STATED, source_id=proposal.source_id,
                    source_contact_id=proposal.source_contact_id,
                    evidence_pointer=proposal.evidence_pointer, trust=proposal.trust,
                    confidence=proposal.confidence, status=FactStatus.ACTIVE,
                    valid_from=proposal.valid_from, valid_to=proposal.valid_to,
                    metadata={**proposal.metadata, "auto_promoted": True, "proposal_id": proposal_id},
                )
                record = self._insert_fact(con, active, timestamp=timestamp)
        return {"proposal_id": proposal_id, "status": state, "deduplicated": False, "fact": record}

    def set_recommendation(self, topic: str, recommendation: str, basis_fact_ids: Sequence[str], *, confidence: float, status: str = "active", change_requirements: Sequence[str] = (), expires_at: float | None = None, idempotency_key: str | None = None, now: float | None = None) -> str:
        if not topic.strip() or not recommendation.strip():
            raise ValueError("topic and recommendation are required")
        if status not in {"proposed", "active", "withdrawn", "fulfilled", "rejected"}:
            raise ValueError("invalid recommendation status")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        timestamp = _finite_timestamp(now)
        if expires_at is not None:
            expires_at = _finite_timestamp(expires_at, name="expires_at")
        recommendation_id = uuid.uuid4().hex
        with self._immediate() as con:
            if idempotency_key:
                duplicate = con.execute("SELECT recommendation_id FROM recommendation WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if duplicate:
                    return str(duplicate[0])
            missing = [fact_id for fact_id in basis_fact_ids if con.execute("SELECT 1 FROM fact WHERE version_id=?", (fact_id,)).fetchone() is None]
            if missing:
                raise ValueError("recommendation basis contains unknown facts")
            prior = con.execute("SELECT recommendation_id FROM recommendation WHERE topic=? AND status='active'", (topic,)).fetchone()
            if status == "active":
                con.execute("UPDATE recommendation SET status='withdrawn',updated_at=? WHERE topic=? AND status='active'", (timestamp, topic))
            con.execute(
                "INSERT INTO recommendation(recommendation_id,topic,recommendation,basis_fact_ids_json,confidence,status,supersedes_id,created_at,updated_at,expires_at,change_requirements_json,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (recommendation_id, topic, recommendation, json.dumps(list(dict.fromkeys(basis_fact_ids))), confidence, status, prior[0] if prior else None, timestamp, timestamp, expires_at, json.dumps(list(dict.fromkeys(change_requirements))), idempotency_key),
            )
        return recommendation_id

    def active_recommendations(self, *, now: float | None = None) -> list[dict[str, object]]:
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            con.execute("UPDATE recommendation SET status='withdrawn',updated_at=? WHERE status='active' AND expires_at IS NOT NULL AND expires_at<=?", (timestamp, timestamp))
            rows = con.execute("SELECT * FROM recommendation WHERE status='active' ORDER BY created_at DESC").fetchall()
            active_rows = []
            for row in rows:
                basis = json.loads(row["basis_fact_ids_json"])
                live = sum(
                    con.execute("SELECT count(*) FROM fact WHERE version_id=? AND status='active' AND tx_to IS NULL", (fact_id,)).fetchone()[0]
                    for fact_id in basis
                )
                if basis and live == len(basis):
                    active_rows.append(row)
                else:
                    con.execute("UPDATE recommendation SET status='withdrawn',updated_at=? WHERE recommendation_id=?", (timestamp, row["recommendation_id"]))
        return [{**dict(row), "basis_fact_ids": json.loads(row["basis_fact_ids_json"]), "change_requirements": json.loads(row["change_requirements_json"])} for row in active_rows]

    def record_callback(self, session_key: str, subject_id: str, *, turn_index: int, subject_type: str = "fact", now: float | None = None) -> None:
        if subject_type not in {"fact", "recommendation"}:
            raise ValueError("invalid callback subject type")
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            con.execute(
                "INSERT OR IGNORE INTO callback_event(event_id,session_key,subject_type,subject_id,turn_index,created_at) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, session_key, subject_type, subject_id, int(turn_index), timestamp),
            )

    def callback_on_cooldown(self, session_key: str, subject_id: str, *, turn_index: int, subject_type: str = "fact", cooldown_turns: int = 10, cooldown_seconds: float = 3600, now: float | None = None) -> bool:
        timestamp = _finite_timestamp(now)
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM callback_event WHERE session_key=? AND subject_type=? AND subject_id=? AND (turn_index>? OR created_at>?) LIMIT 1",
                (session_key, subject_type, subject_id, int(turn_index) - int(cooldown_turns), timestamp - float(cooldown_seconds)),
            ).fetchone()
        return row is not None

    def add_pending(self, proposal: FactProposal, idempotency_key: str) -> str:
        proposal_id = uuid.uuid4().hex
        payload = {
            **proposal.__dict__,
            "audience": proposal.audience.value,
            "mention_policy": proposal.mention_policy.value,
            "assertion_type": proposal.assertion_type.value,
            "status": proposal.status.value,
        }
        with self._immediate() as con:
            con.execute(
                "INSERT OR IGNORE INTO pending_fact(proposal_id,idempotency_key,payload_json,source_id,source_contact_id,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                (proposal_id, idempotency_key, json.dumps(payload, ensure_ascii=False, sort_keys=True), proposal.source_id, proposal.source_contact_id, time.time()),
            )
            row = con.execute("SELECT proposal_id FROM pending_fact WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return str(row[0])

    def list_pending(self) -> list[dict[str, object]]:
        """Return review proposals without changing their lifecycle."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT proposal_id,idempotency_key,payload_json,source_id,status,created_at "
                "FROM pending_fact ORDER BY created_at,proposal_id"
            ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    @staticmethod
    def _proposal_from_payload(payload: dict[str, object]) -> FactProposal:
        values = dict(payload)
        values["audience"] = Audience(str(values.get("audience", Audience.OWNER_ONLY.value)))
        values["mention_policy"] = MentionPolicy(
            str(values.get("mention_policy", MentionPolicy.BACKGROUND.value))
        )
        values["assertion_type"] = AssertionType(
            str(values.get("assertion_type", AssertionType.STATED.value))
        )
        values["status"] = FactStatus(str(values.get("status", FactStatus.PENDING.value)))
        return FactProposal(**values)  # type: ignore[arg-type]

    def decide_pending(
        self,
        proposal_id: str,
        *,
        accept: bool,
        audience: Audience = Audience.OWNER_ONLY,
        mention_policy: MentionPolicy = MentionPolicy.BACKGROUND,
        now: float | None = None,
    ) -> FactRecord | None:
        """Deterministically accept or reject one pending proposal atomically."""
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            row = con.execute(
                "SELECT * FROM pending_fact WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown proposal: {proposal_id}")
            if row["status"] != "pending":
                raise ValueError(f"proposal already {row['status']}")
            record = None
            if accept:
                original = self._proposal_from_payload(json.loads(row["payload_json"]))
                accepted = FactProposal(
                    logical_id=original.logical_id,
                    subject_id=original.subject_id,
                    predicate=original.predicate,
                    object_text=original.object_text,
                    audience=audience,
                    mention_policy=mention_policy,
                    assertion_type=original.assertion_type,
                    source_id=original.source_id,
                    source_contact_id=original.source_contact_id,
                    evidence_pointer=original.evidence_pointer,
                    trust=original.trust,
                    confidence=original.confidence,
                    status=FactStatus.ACTIVE,
                    valid_from=original.valid_from,
                    valid_to=original.valid_to,
                    metadata={**original.metadata, "review_proposal_id": proposal_id},
                )
                record = self._insert_fact(con, accepted, timestamp=timestamp)
            con.execute(
                "UPDATE pending_fact SET status=?,decided_at=? WHERE proposal_id=?",
                ("accepted" if accept else "rejected", timestamp, proposal_id),
            )
        return record

    @staticmethod
    def _row_to_interest_event(row: sqlite3.Row) -> InterestEvent:
        return InterestEvent(
            event_id=str(row["event_id"]),
            topic_text=str(row["topic_text"]),
            signal_type=SignalType(str(row["signal_type"])),
            valence=InterestValence(str(row["valence"])),
            source_id=str(row["source_id"]),
            created_at=float(row["created_at"]),
            folded_at=float(row["folded_at"]) if row["folded_at"] is not None else None,
        )

    @staticmethod
    def _row_to_interest(row: sqlite3.Row) -> Interest:
        return Interest(
            interest_id=str(row["interest_id"]), topic=str(row["topic"]),
            parent_id=str(row["parent_id"]) if row["parent_id"] is not None else None,
            raw_score=float(row["raw_score"]),
            last_evidence_at=float(row["last_evidence_at"]),
            evidence_count=int(row["evidence_count"]),
            valence=InterestValence(str(row["valence"])),
            half_life_days=float(row["half_life_days"]),
            state=InterestState(str(row["state"])),
            ts_alpha=float(row["ts_alpha"]), ts_beta=float(row["ts_beta"]),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            retired_at=float(row["retired_at"]) if row["retired_at"] is not None else None,
        )

    @staticmethod
    def _row_to_proactive_send(row: sqlite3.Row) -> ProactiveSend:
        return ProactiveSend(
            send_id=str(row["send_id"]),
            interest_id=str(row["interest_id"]) if row["interest_id"] is not None else None,
            kind=ProactiveSendKind(str(row["kind"])),
            candidate_json=str(row["candidate_json"]),
            gate_decision=GateDecision(str(row["gate_decision"])),
            gate_reason=str(row["gate_reason"]),
            sent_at=float(row["sent_at"]) if row["sent_at"] is not None else None,
            outcome=ProactiveOutcome(str(row["outcome"])) if row["outcome"] is not None else None,
            outcome_at=float(row["outcome_at"]) if row["outcome_at"] is not None else None,
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _row_to_communication_event(row: sqlite3.Row) -> CommunicationEvent:
        reaction_subtype = (CommunicationReactionSubtype(str(row["reaction_subtype"]))
                            if row["reaction_subtype"] is not None else None)
        values: dict[str, Any] = dict(
            event_id=str(row["event_id"]), platform=str(row["platform"]),
            source_id=str(row["source_id"]), occurred_at=float(row["occurred_at"]),
            direction=CommunicationDirection(str(row["direction"])),
            kind=CommunicationKind(str(row["kind"])),
            actor_role=CommunicationActorRole(str(row["actor_role"])),
            reaction_subtype=reaction_subtype,
            privacy=CommunicationPrivacy(str(row["privacy"])),
            lifecycle=CommunicationLifecycle(str(row["lifecycle"])),
            text_hash=str(row["text_hash"]) if row["text_hash"] is not None else None,
            text_present=bool(row["text_present"]), text_length=int(row["text_length"]),
            provenance=str(row["provenance"]),
            provenance_version=int(row["provenance_version"]),
            retracted_by_event_id=(str(row["retracted_by_event_id"])
                                   if row["retracted_by_event_id"] is not None else None),
        )
        if reaction_subtype is CommunicationReactionSubtype.LEGACY_UNTYPED:
            return CommunicationEvent._from_migrated_legacy(**values)
        return CommunicationEvent(**values)

    @staticmethod
    def _communication_bundle_in(
        con: sqlite3.Connection, event_id: str
    ) -> CommunicationBundle | None:
        row = con.execute(
            "SELECT * FROM communication_event WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        event = ContactMemoryStore._row_to_communication_event(row)
        urls = tuple(CommunicationUrl(
            url_id=str(item["url_id"]), event_id=str(item["event_id"]),
            url_identity=str(item["url_identity"]), domain=str(item["domain"]),
            sharer_role=CommunicationActorRole(str(item["sharer_role"])),
            enrichment_state=CommunicationEnrichmentState(str(item["enrichment_state"])),
            platform=str(item["platform"]) if item["platform"] is not None else None,
        ) for item in con.execute(
            "SELECT * FROM communication_url WHERE event_id=? ORDER BY url_id", (event_id,)
        ))
        attachments = tuple(CommunicationAttachment(
            attachment_id=str(item["attachment_id"]), event_id=str(item["event_id"]),
            attachment_identity=str(item["attachment_identity"]),
            media_kind=str(item["media_kind"]),
            mime_type=str(item["mime_type"]) if item["mime_type"] is not None else None,
            uti=str(item["uti"]) if item["uti"] is not None else None,
            size_bytes=int(item["size_bytes"]) if item["size_bytes"] is not None else None,
            caption_present=bool(item["caption_present"]),
            caption_hash=str(item["caption_hash"]) if item["caption_hash"] is not None else None,
        ) for item in con.execute(
            "SELECT * FROM communication_attachment WHERE event_id=? ORDER BY attachment_id",
            (event_id,),
        ))
        relations = tuple(CommunicationRelation(
            relation_id=str(item["relation_id"]), event_id=str(item["event_id"]),
            relation_type=CommunicationRelationType(str(item["relation_type"])),
            target_source_id=str(item["target_source_id"]),
            target_actor_role=(CommunicationActorRole(str(item["target_actor_role"]))
                               if item["target_actor_role"] is not None else None),
        ) for item in con.execute(
            "SELECT * FROM communication_relation WHERE event_id=? ORDER BY relation_id",
            (event_id,),
        ))
        mentions = tuple(EntityMention(
            mention_id=str(item["mention_id"]), event_id=str(item["event_id"]),
            entity_identity=str(item["entity_identity"]), entity_type=str(item["entity_type"]),
            canonical_label=str(item["canonical_label"]), confidence=float(item["confidence"]),
            source_method=str(item["source_method"]),
            surface_hash=str(item["surface_hash"]) if item["surface_hash"] is not None else None,
        ) for item in con.execute(
            "SELECT * FROM entity_mention WHERE event_id=? ORDER BY mention_id", (event_id,)
        ))
        recommendations = tuple(CommunicationRecommendationEvent(
            recommendation_event_id=str(item["recommendation_event_id"]),
            recommendation_id=str(item["recommendation_id"]), event_id=str(item["event_id"]),
            outcome=CommunicationRecommendationOutcome(str(item["outcome"])),
            confidence=float(item["confidence"]), explicit_linkage=bool(item["explicit_linkage"]),
        ) for item in con.execute(
            "SELECT * FROM communication_recommendation_event WHERE event_id=? "
            "ORDER BY recommendation_event_id", (event_id,)
        ))
        return CommunicationBundle(event, urls, attachments, relations, mentions, recommendations)

    @staticmethod
    def _canonical_communication_bundle(
        event: CommunicationEvent, *, urls: Sequence[CommunicationUrl] = (),
        attachments: Sequence[CommunicationAttachment] = (),
        relations: Sequence[CommunicationRelation] = (),
        entity_mentions: Sequence[EntityMention] = (),
        recommendation_events: Sequence[CommunicationRecommendationEvent] = (),
    ) -> CommunicationBundle:
        groups = (
            ("url", tuple(urls), "url_id"),
            ("attachment", tuple(attachments), "attachment_id"),
            ("relation", tuple(relations), "relation_id"),
            ("entity mention", tuple(entity_mentions), "mention_id"),
            ("recommendation event", tuple(recommendation_events), "recommendation_event_id"),
        )
        for label, items, identity_field in groups:
            if any(item.event_id != event.event_id for item in items):
                raise ValueError(f"{label} child references another communication event")
            identities = [str(getattr(item, identity_field)) for item in items]
            if len(identities) != len(set(identities)):
                raise ValueError(f"duplicate {label} child identity")
        if any(item.sharer_role is not event.actor_role for item in urls):
            raise ValueError("URL sharer role must match the authenticated event actor")
        semantic_groups = (
            ("url", [(item.event_id, item.url_identity) for item in urls]),
            ("attachment", [(item.event_id, item.attachment_identity) for item in attachments]),
            ("relation", [(item.event_id, item.relation_type, item.target_source_id) for item in relations]),
            ("entity mention", [(item.event_id, item.entity_identity, item.source_method)
                                for item in entity_mentions]),
            ("recommendation event", [(item.recommendation_id, item.event_id, item.outcome)
                                      for item in recommendation_events]),
        )
        for label, keys in semantic_groups:
            if len(keys) != len(set(keys)):
                raise ValueError(f"duplicate {label} semantic key")
        return CommunicationBundle(
            event=event,
            urls=tuple(sorted(urls, key=lambda item: item.url_id)),
            attachments=tuple(sorted(attachments, key=lambda item: item.attachment_id)),
            relations=tuple(sorted(relations, key=lambda item: item.relation_id)),
            entity_mentions=tuple(sorted(entity_mentions, key=lambda item: item.mention_id)),
            recommendation_events=tuple(sorted(
                recommendation_events, key=lambda item: item.recommendation_event_id
            )),
        )

    @classmethod
    def _ingest_communication_bundle_in(
        cls, con: sqlite3.Connection, bundle: CommunicationBundle
    ) -> bool:
        existing = cls._communication_bundle_in(con, bundle.event.event_id)
        if existing is not None:
            if not cls._communication_replay_matches(existing, bundle):
                raise ValueError("event_id already belongs to a different communication event")
            return False
        source = con.execute(
            "SELECT event_id FROM communication_event WHERE platform=? AND source_id=?",
            (bundle.event.platform, bundle.event.source_id),
        ).fetchone()
        if source is not None:
            raise ValueError("platform source ID already belongs to another communication event")
        event = bundle.event
        con.execute(
            """INSERT INTO communication_event(
              event_id,platform,source_id,occurred_at,direction,kind,reaction_subtype,
              actor_role,privacy,
              lifecycle,text_hash,text_present,text_length,provenance,provenance_version,
              retracted_by_event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event.event_id, event.platform, event.source_id, event.occurred_at,
             event.direction.value, event.kind.value,
             event.reaction_subtype.value if event.reaction_subtype is not None else None,
             event.actor_role.value,
             event.privacy.value, event.lifecycle.value, event.text_hash,
             int(event.text_present), event.text_length, event.provenance,
             event.provenance_version, event.retracted_by_event_id),
        )
        cls._insert_communication_children_in(con, bundle)
        if event.kind is CommunicationKind.REACTION_ADD:
            cls._apply_pending_retraction_in(con, bundle)
        return True

    @staticmethod
    def _communication_replay_matches(
        stored: CommunicationBundle, replay: CommunicationBundle
    ) -> bool:
        stored_event = stored.event
        if stored_event.lifecycle is CommunicationLifecycle.RETRACTED:
            stored_event = replace(
                stored_event,
                lifecycle=CommunicationLifecycle.ACTIVE,
                retracted_by_event_id=None,
            )
        if stored_event != replay.event:
            return False

        def contains_all(stored_items: Sequence[Any], replay_items: Sequence[Any], field: str) -> bool:
            by_id = {str(getattr(item, field)): item for item in stored_items}
            return all(by_id.get(str(getattr(item, field))) == item for item in replay_items)

        stored_urls = {item.url_id: item for item in stored.urls}
        for item in replay.urls:
            prior = stored_urls.get(item.url_id)
            if prior == item:
                continue
            if not (
                prior is not None
                and item.enrichment_state is CommunicationEnrichmentState.PENDING
                and prior.enrichment_state is not CommunicationEnrichmentState.PENDING
                and replace(prior, enrichment_state=CommunicationEnrichmentState.PENDING) == item
            ):
                return False
        return (
            contains_all(stored.attachments, replay.attachments, "attachment_id")
            and contains_all(stored.relations, replay.relations, "relation_id")
            and contains_all(stored.entity_mentions, replay.entity_mentions, "mention_id")
            and contains_all(
                stored.recommendation_events, replay.recommendation_events,
                "recommendation_event_id",
            )
        )

    @staticmethod
    def _insert_communication_children_in(
        con: sqlite3.Connection, bundle: CommunicationBundle
    ) -> None:
        for item in bundle.urls:
            con.execute("INSERT INTO communication_url VALUES(?,?,?,?,?,?,?)", (
                item.url_id, item.event_id, item.url_identity, item.domain,
                item.sharer_role.value, item.enrichment_state.value, item.platform,
            ))
        for item in bundle.attachments:
            con.execute("INSERT INTO communication_attachment VALUES(?,?,?,?,?,?,?,?,?)", (
                item.attachment_id, item.event_id, item.attachment_identity, item.media_kind,
                item.mime_type, item.uti, item.size_bytes, int(item.caption_present), item.caption_hash,
            ))
        for item in bundle.relations:
            con.execute("INSERT INTO communication_relation VALUES(?,?,?,?,?)", (
                item.relation_id, item.event_id, item.relation_type.value,
                item.target_source_id,
                item.target_actor_role.value if item.target_actor_role is not None else None,
            ))
        for item in bundle.entity_mentions:
            con.execute("INSERT INTO entity_mention VALUES(?,?,?,?,?,?,?,?)", (
                item.mention_id, item.event_id, item.entity_identity, item.entity_type,
                item.canonical_label, item.confidence, item.source_method, item.surface_hash,
            ))
        for item in bundle.recommendation_events:
            con.execute("INSERT INTO communication_recommendation_event VALUES(?,?,?,?,?,?)", (
                item.recommendation_event_id, item.recommendation_id, item.event_id,
                item.outcome.value, item.confidence, int(item.explicit_linkage),
            ))

    def ingest_communication_event(
        self, event: CommunicationEvent, *, urls: Sequence[CommunicationUrl] = (),
        attachments: Sequence[CommunicationAttachment] = (),
        relations: Sequence[CommunicationRelation] = (),
        entity_mentions: Sequence[EntityMention] = (),
        recommendation_events: Sequence[CommunicationRecommendationEvent] = (),
    ) -> CommunicationIngestResult:
        """Atomically append one authenticated event and its typed evidence."""
        if event.reaction_subtype is CommunicationReactionSubtype.LEGACY_UNTYPED:
            raise ValueError("legacy_untyped is reserved for migrated historical reactions")
        if event.kind is CommunicationKind.REACTION_REMOVE:
            raise ValueError("reaction removals require retract_communication_event")
        if event.lifecycle is not CommunicationLifecycle.ACTIVE or event.retracted_by_event_id is not None:
            raise ValueError("new communication events must be active")
        bundle = self._canonical_communication_bundle(
            event, urls=urls, attachments=attachments, relations=relations,
            entity_mentions=entity_mentions, recommendation_events=recommendation_events,
        )
        if event.kind is CommunicationKind.REACTION_ADD:
            self._reaction_relation(bundle)
        try:
            with self._immediate() as con:
                inserted = self._ingest_communication_bundle_in(con, bundle)
        except sqlite3.IntegrityError as exc:
            raise ValueError("communication child conflicts with stored evidence") from exc
        persisted = self.get_communication_event(event.event_id)
        assert persisted is not None
        return CommunicationIngestResult(event=persisted, inserted=inserted, deduplicated=not inserted)

    def enrich_communication_event(
        self, event_id: str, *, urls: Sequence[CommunicationUrl] = (),
        attachments: Sequence[CommunicationAttachment] = (),
        relations: Sequence[CommunicationRelation] = (),
        entity_mentions: Sequence[EntityMention] = (),
        recommendation_events: Sequence[CommunicationRecommendationEvent] = (),
    ) -> CommunicationBundle:
        """Atomically add typed child evidence without rewriting source ingress."""
        identity = str(event_id)
        try:
            with self._immediate() as con:
                existing = self._communication_bundle_in(con, identity)
                if existing is None:
                    raise KeyError(f"unknown communication event: {identity}")
                incoming = self._canonical_communication_bundle(
                    existing.event, urls=urls, attachments=attachments, relations=relations,
                    entity_mentions=entity_mentions,
                    recommendation_events=recommendation_events,
                )

                existing_urls = {item.url_id: item for item in existing.urls}
                url_additions: list[CommunicationUrl] = []
                url_updates: list[CommunicationUrl] = []
                merged_urls = dict(existing_urls)
                for item in incoming.urls:
                    prior = existing_urls.get(item.url_id)
                    if prior is None:
                        url_additions.append(item)
                        merged_urls[item.url_id] = item
                    elif prior == item:
                        continue
                    elif (
                        replace(prior, enrichment_state=item.enrichment_state) == item
                        and prior.enrichment_state is CommunicationEnrichmentState.PENDING
                        and item.enrichment_state is not CommunicationEnrichmentState.PENDING
                    ):
                        url_updates.append(item)
                        merged_urls[item.url_id] = item
                    else:
                        raise ValueError("URL child identity conflicts with stored evidence")

                def additions(existing_items: Sequence[Any], incoming_items: Sequence[Any], field: str) -> list[Any]:
                    by_id = {str(getattr(item, field)): item for item in existing_items}
                    result: list[Any] = []
                    for item in incoming_items:
                        prior = by_id.get(str(getattr(item, field)))
                        if prior is None:
                            result.append(item)
                        elif prior != item:
                            raise ValueError(f"{field} conflicts with stored evidence")
                    return result

                attachment_additions = additions(
                    existing.attachments, incoming.attachments, "attachment_id"
                )
                relation_additions = additions(existing.relations, incoming.relations, "relation_id")
                mention_additions = additions(
                    existing.entity_mentions, incoming.entity_mentions, "mention_id"
                )
                recommendation_additions = additions(
                    existing.recommendation_events, incoming.recommendation_events,
                    "recommendation_event_id",
                )
                combined = self._canonical_communication_bundle(
                    existing.event,
                    urls=tuple(merged_urls.values()),
                    attachments=(*existing.attachments, *attachment_additions),
                    relations=(*existing.relations, *relation_additions),
                    entity_mentions=(*existing.entity_mentions, *mention_additions),
                    recommendation_events=(
                        *existing.recommendation_events, *recommendation_additions
                    ),
                )
                if existing.event.kind in {
                    CommunicationKind.REACTION_ADD, CommunicationKind.REACTION_REMOVE
                }:
                    self._reaction_relation(combined)
                additions_bundle = CommunicationBundle(
                    event=existing.event, urls=tuple(url_additions),
                    attachments=tuple(attachment_additions), relations=tuple(relation_additions),
                    entity_mentions=tuple(mention_additions),
                    recommendation_events=tuple(recommendation_additions),
                )
                self._insert_communication_children_in(con, additions_bundle)
                for item in url_updates:
                    con.execute(
                        "UPDATE communication_url SET enrichment_state=? WHERE url_id=?",
                        (item.enrichment_state.value, item.url_id),
                    )
                return combined
        except sqlite3.IntegrityError as exc:
            raise ValueError("communication child conflicts with stored evidence") from exc

    def get_communication_event(self, event_id: str) -> CommunicationEvent | None:
        with self._connect() as con:
            bundle = self._communication_bundle_in(con, str(event_id))
        return bundle.event if bundle is not None else None

    def get_communication_event_by_source(
        self, platform: str, source_id: str
    ) -> CommunicationEvent | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM communication_event WHERE platform=? AND source_id=?",
                (str(platform), str(source_id)),
            ).fetchone()
        return self._row_to_communication_event(row) if row is not None else None

    def get_communication_bundle(self, event_id: str) -> CommunicationBundle | None:
        with self._connect() as con:
            return self._communication_bundle_in(con, str(event_id))

    def find_latest_reaction_add(
        self,
        *,
        platform: str,
        reaction_subtype: CommunicationReactionSubtype,
        target_source_id: str,
    ) -> str | None:
        """Resolve the latest active add for one authenticated reaction target."""
        if not isinstance(reaction_subtype, CommunicationReactionSubtype):
            raise ValueError("reaction_subtype is invalid")
        if reaction_subtype is CommunicationReactionSubtype.LEGACY_UNTYPED:
            raise ValueError("legacy reactions cannot be live retraction targets")
        if not re.fullmatch(r"[0-9a-f]{64}", str(target_source_id)):
            raise ValueError("target_source_id must be an opaque identity")
        with self._connect() as con:
            row = con.execute(
                """SELECT e.event_id
                   FROM communication_event e
                   JOIN communication_relation r ON r.event_id=e.event_id
                   WHERE e.platform=? AND e.kind='reaction_add'
                     AND e.reaction_subtype=? AND e.lifecycle='active'
                     AND r.relation_type='reaction_to' AND r.target_source_id=?
                   ORDER BY e.occurred_at DESC,e.event_id DESC LIMIT 1""",
                (str(platform), reaction_subtype.value, str(target_source_id)),
            ).fetchone()
        return str(row["event_id"]) if row is not None else None

    @staticmethod
    def _reaction_relation(bundle: CommunicationBundle) -> CommunicationRelation:
        matches = [
            relation for relation in bundle.relations
            if relation.relation_type is CommunicationRelationType.REACTION_TO
        ]
        if len(matches) != 1:
            raise ValueError("reaction events require exactly one reaction target relation")
        return matches[0]

    @classmethod
    def _validate_retraction_pair(
        cls, target: CommunicationBundle, removal: CommunicationBundle
    ) -> None:
        if target.event.kind is not CommunicationKind.REACTION_ADD:
            raise ValueError("retraction target is not a reaction add")
        if target.event.reaction_subtype is not removal.event.reaction_subtype:
            raise ValueError("retraction reaction subtype does not match the target")
        if (
            target.event.platform != removal.event.platform
            or target.event.actor_role is not removal.event.actor_role
            or target.event.direction is not removal.event.direction
        ):
            raise ValueError("retraction actor or platform does not match the target")
        target_relation = cls._reaction_relation(target)
        removal_relation = cls._reaction_relation(removal)
        if (
            target_relation.target_source_id != removal_relation.target_source_id
            or target_relation.target_actor_role is not removal_relation.target_actor_role
        ):
            raise ValueError("retraction target relation does not match")

    @classmethod
    def _apply_pending_retraction_in(
        cls, con: sqlite3.Connection, target: CommunicationBundle
    ) -> None:
        pending = con.execute(
            "SELECT retraction_event_id FROM communication_retraction_pending "
            "WHERE target_event_id=?", (target.event.event_id,),
        ).fetchone()
        if pending is None:
            return
        removal = cls._communication_bundle_in(con, str(pending["retraction_event_id"]))
        if removal is None:
            raise RuntimeError("pending retraction references a missing event")
        cls._validate_retraction_pair(target, removal)
        con.execute(
            "UPDATE communication_event SET lifecycle='retracted',retracted_by_event_id=? "
            "WHERE event_id=? AND lifecycle='active'",
            (removal.event.event_id, target.event.event_id),
        )
        con.execute(
            "DELETE FROM communication_retraction_pending WHERE retraction_event_id=?",
            (removal.event.event_id,),
        )

    def retract_communication_event(
        self, event: CommunicationEvent, *, target_event_id: str,
        relations: Sequence[CommunicationRelation],
    ) -> CommunicationIngestResult:
        """Append a reaction removal and retract its authenticated target atomically."""
        if event.reaction_subtype is CommunicationReactionSubtype.LEGACY_UNTYPED:
            raise ValueError("legacy_untyped is reserved for migrated historical reactions")
        if (
            event.kind is not CommunicationKind.REACTION_REMOVE
            or event.lifecycle is not CommunicationLifecycle.ACTIVE
            or event.retracted_by_event_id is not None
        ):
            raise ValueError("retraction event must be a reaction removal")
        bundle = self._canonical_communication_bundle(event, relations=relations)
        self._reaction_relation(bundle)
        target_identity = str(target_event_id)
        if not re.fullmatch(r"[0-9a-f]{64}", target_identity):
            raise ValueError("target_event_id must be a lowercase opaque identity")
        try:
            with self._immediate() as con:
                applied = con.execute(
                    "SELECT event_id FROM communication_event WHERE retracted_by_event_id=?",
                    (event.event_id,),
                ).fetchone()
                if applied is not None and str(applied["event_id"]) != target_identity:
                    raise ValueError("retraction event already belongs to another target")
                pending = con.execute(
                    "SELECT target_event_id FROM communication_retraction_pending "
                    "WHERE retraction_event_id=?", (event.event_id,),
                ).fetchone()
                if pending is not None and str(pending["target_event_id"]) != target_identity:
                    raise ValueError("retraction event already belongs to another target")
                target = self._communication_bundle_in(con, target_identity)
                if target is None:
                    inserted = self._ingest_communication_bundle_in(con, bundle)
                    con.execute(
                        "INSERT INTO communication_retraction_pending("
                        "retraction_event_id,target_event_id) VALUES(?,?) "
                        "ON CONFLICT(retraction_event_id) DO NOTHING",
                        (event.event_id, target_identity),
                    )
                    return CommunicationIngestResult(
                        event=event, inserted=inserted, deduplicated=not inserted
                    )
                self._validate_retraction_pair(target, bundle)
                prior_retractor = target.event.retracted_by_event_id
                if prior_retractor is not None and prior_retractor != event.event_id:
                    raise ValueError("target was retracted by another event")
                inserted = self._ingest_communication_bundle_in(con, bundle)
                if prior_retractor is None:
                    con.execute(
                        "UPDATE communication_event SET lifecycle='retracted',"
                        "retracted_by_event_id=? WHERE event_id=? AND lifecycle='active'",
                        (event.event_id, target_identity),
                    )
                con.execute(
                    "DELETE FROM communication_retraction_pending WHERE retraction_event_id=?",
                    (event.event_id,),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("communication child conflicts with stored evidence") from exc
        return CommunicationIngestResult(event=event, inserted=inserted, deduplicated=not inserted)

    @staticmethod
    def _candidate_item_hash(candidate_json: str) -> str | None:
        """Extract the normalized novelty key from a strict or historical payload."""
        try:
            candidate = json.loads(candidate_json)
        except (TypeError, json.JSONDecodeError):
            return None
        concrete_item = candidate.get("concrete_item") if isinstance(candidate, dict) else None
        if not isinstance(concrete_item, str) or not concrete_item.strip():
            return None
        return normalized_proactive_item_hash(concrete_item)

    def record_interest_event(
        self,
        *,
        topic_text: object,
        signal_type: SignalType | str,
        valence: InterestValence | str,
        source_id: str,
        now: float | None = None,
        event_id: str | None = None,
    ) -> InterestEvent:
        """Validate and idempotently append one text-free interest signal."""
        topic = normalize_interest_topic(topic_text)
        signal = SignalType(_enum_text(signal_type))
        polarity = InterestValence(_enum_text(valence))
        source = str(source_id).strip()
        if not source or len(source) > 500 or any(ord(ch) < 32 for ch in source):
            raise ValueError("source_id is invalid")
        identity = event_id or hashlib.sha256(
            "\0".join((source, topic, signal.value, polarity.value)).encode()
        ).hexdigest()
        if not str(identity).strip():
            raise ValueError("event_id is required")
        timestamp = _finite_timestamp(now)
        if not math.isfinite(timestamp):
            raise ValueError("event timestamp must be finite")
        with self._immediate() as con:
            con.execute(
                "INSERT OR IGNORE INTO interest_event("
                "event_id,topic_text,signal_type,valence,source_id,created_at,folded_at"
                ") VALUES(?,?,?,?,?,?,NULL)",
                (identity, topic, signal.value, polarity.value, source, timestamp),
            )
            row = con.execute(
                "SELECT * FROM interest_event WHERE event_id=?", (identity,)
            ).fetchone()
        assert row is not None
        event = self._row_to_interest_event(row)
        if (
            event.topic_text != topic or event.signal_type is not signal
            or event.valence is not polarity or event.source_id != source
        ):
            raise ValueError("event_id already belongs to a different interest event")
        return event

    def interest_evidence_days(self, topic_text: object) -> int:
        """Count distinct UTC evidence days for a topic across all its events.

        Promotion requires evidence spread over separate days, which a single
        chatty session must never satisfy on its own. The count spans folded and
        unfolded rows so a promotion decision is stable across maintenance runs.
        """
        topic = normalize_interest_topic(topic_text)
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT CAST(created_at / 86400 AS INTEGER) AS day "
                "FROM interest_event WHERE topic_text=?",
                (topic,),
            ).fetchall()
        return len(rows)

    def unfolded_interest_events(self, *, limit: int | None = None) -> list[InterestEvent]:
        sql = "SELECT * FROM interest_event WHERE folded_at IS NULL ORDER BY created_at,event_id"
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, int(limit)),)
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row_to_interest_event(row) for row in rows]

    def mark_interest_events_folded(
        self, event_ids: Iterable[str], *, now: float | None = None
    ) -> int:
        ids = list(dict.fromkeys(str(value) for value in event_ids if str(value)))
        if not ids:
            return 0
        timestamp = _finite_timestamp(now)
        changed = 0
        with self._immediate() as con:
            for event_id in ids:
                changed += con.execute(
                    "UPDATE interest_event SET folded_at=? WHERE event_id=? AND folded_at IS NULL",
                    (timestamp, event_id),
                ).rowcount
        return changed

    def _fold_unfolded_interest_events_in_transaction(
        self, con: sqlite3.Connection, *, timestamp: float
    ) -> dict[str, object]:
        """Fold pending events using an already-open maintenance transaction."""
        from .schema import INTEREST_SIGNAL_BANDIT, INTEREST_SIGNAL_WEIGHTS

        affected: dict[str, str] = {}
        folded = 0
        rows = con.execute(
            "SELECT event_id,topic_text,signal_type,valence,created_at "
            "FROM interest_event WHERE folded_at IS NULL "
            "ORDER BY created_at,event_id"
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["topic_text"]), []).append(row)
        for topic, events in grouped.items():
            existing = con.execute(
                "SELECT * FROM interest WHERE topic=? AND retired_at IS NULL",
                (topic,),
            ).fetchone()
            score_delta = 0.0
            alpha_delta = 0.0
            beta_delta = 0.0
            latest = 0.0
            turned_negative = False
            for event in events:
                signal = SignalType(str(event["signal_type"]))
                score_delta += INTEREST_SIGNAL_WEIGHTS.get(signal, 0.0)
                da, db = INTEREST_SIGNAL_BANDIT.get(signal, (0.0, 0.0))
                alpha_delta += da
                beta_delta += db
                latest = max(latest, float(event["created_at"]))
                if signal is SignalType.EXPLICIT_NEGATIVE:
                    turned_negative = True
            if existing is None:
                interest_id = uuid.uuid4().hex
                valence = (
                    InterestValence.NEGATIVE.value if turned_negative
                    else InterestValence.POSITIVE.value
                )
                con.execute(
                    """INSERT INTO interest(
                      interest_id,topic,parent_id,raw_score,last_evidence_at,evidence_count,
                      valence,half_life_days,state,ts_alpha,ts_beta,created_at,updated_at,retired_at
                    ) VALUES(?,?,NULL,?,?,?,?,?, 'candidate', ?,?,?,?,NULL)""",
                    (
                        interest_id, topic, score_delta, latest, len(events),
                        valence, 90.0, max(1.0, 1.0 + alpha_delta),
                        max(1.0, 1.0 + beta_delta), timestamp, timestamp,
                    ),
                )
                affected[interest_id] = topic
            else:
                interest_id = str(existing["interest_id"])
                new_score = float(existing["raw_score"]) + score_delta
                new_count = int(existing["evidence_count"]) + len(events)
                new_last = max(float(existing["last_evidence_at"]), latest)
                valence = (
                    InterestValence.NEGATIVE.value
                    if turned_negative or existing["valence"] == InterestValence.NEGATIVE.value
                    else str(existing["valence"])
                )
                new_alpha = max(0.0001, float(existing["ts_alpha"]) + alpha_delta)
                new_beta = max(0.0001, float(existing["ts_beta"]) + beta_delta)
                con.execute(
                    "UPDATE interest SET raw_score=?,evidence_count=?,last_evidence_at=?,"
                    "valence=?,ts_alpha=?,ts_beta=?,updated_at=? WHERE interest_id=?",
                    (
                        new_score, new_count, new_last, valence,
                        new_alpha, new_beta, timestamp, interest_id,
                    ),
                )
                affected[interest_id] = topic
            for event in events:
                folded += con.execute(
                    "UPDATE interest_event SET folded_at=? WHERE event_id=? AND folded_at IS NULL",
                    (timestamp, event["event_id"]),
                ).rowcount
        return {"folded_events": folded, "affected_interests": affected}

    def fold_unfolded_interest_events(
        self, *, now: float | None = None, maintenance_claim_id: str | None = None
    ) -> dict[str, object]:
        """Deterministically fold every unfolded signal into ledger raw scores.

        This is the maintenance "step 1" from the plan. It runs in one writer
        transaction so a crash cannot half-apply, and it is idempotent: the
        ``folded_at`` guard means a rerun after a completed fold consumes nothing
        and mutates nothing. New topic strings materialize as ``candidate``
        interests; ``explicit_negative`` flips valence to a permanent block.
        Score/bandit deltas come from ``schema.INTEREST_SIGNAL_WEIGHTS`` /
        ``INTEREST_SIGNAL_BANDIT`` — the single source of truth for weights.
        """
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            claim_state: dict[str, object] = {}
            if maintenance_claim_id is not None:
                claim_state = self._interest_maintenance_state_in(con)
                if (
                    claim_state.get("status") != "running"
                    or claim_state.get("claim_id") != maintenance_claim_id
                ):
                    raise RuntimeError("interest maintenance claim is not current")
            result = self._fold_unfolded_interest_events_in_transaction(
                con, timestamp=timestamp
            )
            if maintenance_claim_id is not None:
                folded = int(result["folded_events"])
                claim_state.update({
                    "phase": "folded",
                    "updated_at": timestamp,
                    "folded_events": int(claim_state.get("folded_events", 0)) + folded,
                })
                self._write_interest_maintenance_state_in(con, claim_state)
            return result

    _MAINTENANCE_META_KEY = "interest_maintenance_v1"

    @classmethod
    def _interest_maintenance_state_in(cls, con: sqlite3.Connection) -> dict[str, object]:
        row = con.execute(
            "SELECT value FROM schema_meta WHERE key=?", (cls._MAINTENANCE_META_KEY,)
        ).fetchone()
        if not row:
            return {}
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _write_interest_maintenance_state_in(
        cls, con: sqlite3.Connection, payload: dict[str, object]
    ) -> None:
        con.execute(
            "INSERT INTO schema_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cls._MAINTENANCE_META_KEY, json.dumps(payload, separators=(",", ":"))),
        )

    def interest_maintenance_state(self) -> dict[str, object]:
        """Return durable per-contact maintenance claim/run bookkeeping."""
        with self._connect() as con:
            return self._interest_maintenance_state_in(con)

    def claim_interest_maintenance(
        self,
        *,
        now: float | None = None,
        force: bool = False,
        min_unfolded_events: int = 20,
        max_run_age_seconds: float = 7 * 86_400.0,
        lease_seconds: float = 15 * 60.0,
    ) -> dict[str, object] | None:
        """Atomically claim a run, or recover a stale folded-but-uncompleted run."""
        timestamp = _finite_timestamp(now)
        lease = max(1.0, float(lease_seconds))
        with self._immediate() as con:
            state = self._interest_maintenance_state_in(con)
            running = state.get("status") == "running"
            updated = state.get("updated_at", state.get("started_at"))
            if running and isinstance(updated, (int, float)) and timestamp - float(updated) < lease:
                return None
            unfolded = int(con.execute(
                "SELECT count(*) FROM interest_event WHERE folded_at IS NULL"
            ).fetchone()[0])
            last_run = state.get("last_run_at")
            recovery = running
            due = (
                force
                or recovery
                or unfolded >= max(1, int(min_unfolded_events))
                or (unfolded > 0 and not isinstance(last_run, (int, float)))
                or (
                    isinstance(last_run, (int, float))
                    and timestamp - float(last_run) >= float(max_run_age_seconds)
                )
            )
            if not due:
                return None
            claim: dict[str, object] = {
                "status": "running",
                "phase": "claimed",
                "claim_id": uuid.uuid4().hex,
                "generation": int(state.get("generation", 0)) + 1,
                "started_at": timestamp,
                "updated_at": timestamp,
                "folded_events": int(state.get("folded_events", 0)) if recovery else 0,
            }
            if isinstance(last_run, (int, float)):
                claim["last_run_at"] = float(last_run)
            self._write_interest_maintenance_state_in(con, claim)
            return claim

    def complete_interest_maintenance(
        self, claim_id: str, *, now: float | None = None
    ) -> dict[str, object]:
        """Complete only the current claim; stale workers cannot overwrite it."""
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            state = self._interest_maintenance_state_in(con)
            if state.get("status") != "running" or state.get("claim_id") != claim_id:
                raise RuntimeError("interest maintenance claim is not current")
            payload: dict[str, object] = {
                "status": "completed",
                "phase": "completed",
                "generation": int(state.get("generation", 0)),
                "last_run_at": timestamp,
                "last_folded_events": int(state.get("folded_events", 0)),
                "completed_at": timestamp,
            }
            self._write_interest_maintenance_state_in(con, payload)
            return payload

    def record_interest_maintenance_run(
        self, *, now: float | None = None, folded_events: int = 0
    ) -> dict[str, object]:
        """Compatibility helper for importing historical completed-run state."""
        timestamp = _finite_timestamp(now)
        payload: dict[str, object] = {
            "status": "completed", "phase": "completed", "generation": 0,
            "last_run_at": timestamp, "last_folded_events": int(folded_events),
            "completed_at": timestamp,
        }
        with self._immediate() as con:
            self._write_interest_maintenance_state_in(con, payload)
        return payload

    def _apply_deterministic_interest_maintenance_in(
        self, con: sqlite3.Connection, *, timestamp: float
    ) -> dict[str, list[str]]:
        """Apply lifecycle and iterative cap using an existing writer transaction."""
        promoted: list[str] = []
        retired: list[str] = []
        pruned: list[str] = []
        rows = con.execute("SELECT * FROM interest WHERE retired_at IS NULL").fetchall()
        for row in rows:
            # An earlier parent cascade may already have retired this snapshot row.
            if con.execute(
                "SELECT 1 FROM interest WHERE interest_id=? AND retired_at IS NULL",
                (row["interest_id"],),
            ).fetchone() is None:
                continue
            item = self._row_to_interest(row)
            effective = item.effective_score(timestamp)
            if item.state is InterestState.CANDIDATE:
                days = int(con.execute(
                    "SELECT count(DISTINCT CAST(created_at / 86400 AS INTEGER)) "
                    "FROM interest_event WHERE topic_text=?", (item.topic,),
                ).fetchone()[0])
                if effective >= 1.5 and days >= 2:
                    con.execute(
                        "UPDATE interest SET state='active',updated_at=? WHERE interest_id=?",
                        (timestamp, item.interest_id),
                    )
                    promoted.append(item.interest_id)
            elif item.state is InterestState.ACTIVE:
                stale = timestamp - item.last_evidence_at >= 2 * item.half_life_days * 86_400.0
                if effective < 0.2 and stale:
                    subtree = con.execute(
                        """WITH RECURSIVE descendants(interest_id,depth) AS (
                          SELECT interest_id,0 FROM interest WHERE interest_id=? AND retired_at IS NULL
                          UNION ALL
                          SELECT child.interest_id,descendants.depth+1 FROM interest child
                          JOIN descendants ON child.parent_id=descendants.interest_id
                          WHERE child.retired_at IS NULL
                        ) SELECT interest_id FROM descendants ORDER BY depth DESC,interest_id""",
                        (item.interest_id,),
                    ).fetchall()
                    for descendant in subtree:
                        descendant_id = str(descendant["interest_id"])
                        con.execute(
                            "UPDATE interest SET state='retired',retired_at=?,updated_at=? "
                            "WHERE interest_id=? AND retired_at IS NULL",
                            (timestamp, timestamp, descendant_id),
                        )
                        retired.append(descendant_id)

        # Remove one current leaf per iteration. A parent cannot be selected while
        # it has a live child, so every committed intermediate graph stays valid.
        while True:
            live_rows = con.execute("SELECT * FROM interest WHERE retired_at IS NULL").fetchall()
            if len(live_rows) <= INTEREST_MAX_LIVE_TOPICS:
                break
            parent_ids = {
                str(row["parent_id"]) for row in live_rows if row["parent_id"] is not None
            }
            leaves = [
                self._row_to_interest(row) for row in live_rows
                if str(row["interest_id"]) not in parent_ids
            ]
            if not leaves:
                raise RuntimeError("interest taxonomy has no prunable leaf")
            leaf = min(leaves, key=lambda item: (item.effective_score(timestamp), item.interest_id))
            con.execute(
                "UPDATE interest SET state='retired',retired_at=?,updated_at=? WHERE interest_id=?",
                (timestamp, timestamp, leaf.interest_id),
            )
            pruned.append(leaf.interest_id)
        if con.execute(
            """SELECT 1 FROM interest child JOIN interest parent
               ON parent.interest_id=child.parent_id
               WHERE child.retired_at IS NULL AND parent.retired_at IS NOT NULL LIMIT 1"""
        ).fetchone() is not None:
            raise RuntimeError("maintenance would commit an orphaned taxonomy child")
        return {"promoted": promoted, "retired": retired, "pruned": pruned}

    def apply_interest_maintenance_batch(
        self,
        *,
        merges: Sequence[tuple[str, str]] = (),
        splits: Sequence[tuple[str, Sequence[tuple[str, str]]]] = (),
        half_lives: dict[str, float] | None = None,
        now: float | None = None,
        maintenance_claim_id: str | None = None,
    ) -> dict[str, list[str]]:
        """Validate and apply one complete proposal under one BEGIN IMMEDIATE.

        Validation is deliberately repeated inside the writer lock. The model's
        earlier snapshot can be stale; only this graph check is authoritative.
        Any invalid edge, duplicate target, structural conflict, topic collision,
        or projected cap overflow raises before mutation. Any later SQLite error
        rolls the entire batch back through ``_immediate``.
        """
        timestamp = _finite_timestamp(now)
        half_lives = dict(half_lives or {})
        merge_list = [(str(a), str(b)) for a, b in merges]
        split_list = [(str(parent), [(str(cid), str(topic)) for cid, topic in children])
                      for parent, children in splits]
        if len(merge_list) > INTEREST_MAX_LIVE_TOPICS:
            raise ValueError("merges must be a bounded list")
        if len(split_list) > 12:
            raise ValueError("splits must be a bounded list")
        if len(half_lives) > INTEREST_MAX_LIVE_TOPICS:
            raise ValueError("half-lives must be a bounded mapping")
        with self._immediate() as con:
            if maintenance_claim_id is not None:
                claim_state = self._interest_maintenance_state_in(con)
                if (
                    claim_state.get("status") != "running"
                    or claim_state.get("claim_id") != maintenance_claim_id
                ):
                    raise RuntimeError("interest maintenance claim is not current")
            all_rows = con.execute("SELECT * FROM interest").fetchall()
            rows = [row for row in all_rows if row["retired_at"] is None]
            by_id = {str(row["interest_id"]): row for row in rows}
            live_ids = set(by_id)
            historical_ids = {str(row["interest_id"]) for row in all_rows}
            # Retired topics remain reserved: replaying a deterministic proposal
            # must not silently reuse a historical taxonomy identity.
            existing_topics = {
                str(row["topic"]): str(row["interest_id"]) for row in all_rows
            }
            if con.execute(
                """SELECT 1 FROM interest child LEFT JOIN interest parent
                   ON parent.interest_id=child.parent_id
                   WHERE child.retired_at IS NULL AND child.parent_id IS NOT NULL
                     AND (parent.interest_id IS NULL OR parent.retired_at IS NOT NULL
                          OR parent.parent_id IS NOT NULL) LIMIT 1"""
            ).fetchone() is not None:
                raise ValueError("existing live interest taxonomy is invalid")

            used_merge_ids: set[str] = set()
            absorbed: set[str] = set()
            for keep_id, absorb_id in merge_list:
                if keep_id == absorb_id or keep_id not in live_ids or absorb_id not in live_ids:
                    raise ValueError("merge references invalid live interests")
                if keep_id in used_merge_ids or absorb_id in used_merge_ids:
                    raise ValueError("an interest may appear in only one merge")
                keep, absorb = by_id[keep_id], by_id[absorb_id]
                if keep["valence"] != absorb["valence"]:
                    raise ValueError("cannot merge across valence polarity")
                if keep["parent_id"] != absorb["parent_id"]:
                    raise ValueError("merges must stay within one taxonomy level and parent")
                if con.execute(
                    "SELECT 1 FROM interest WHERE parent_id=? AND retired_at IS NULL LIMIT 1",
                    (absorb_id,),
                ).fetchone() is not None:
                    raise ValueError("cannot absorb a parent with live children")
                used_merge_ids.update((keep_id, absorb_id))
                absorbed.add(absorb_id)

            split_parents: set[str] = set()
            new_ids: set[str] = set()
            new_topics: set[str] = set()
            child_total = 0
            for parent_id, children in split_list:
                if parent_id not in live_ids or parent_id in split_parents:
                    raise ValueError("split parent must be unique and live")
                if parent_id in used_merge_ids or parent_id in half_lives:
                    raise ValueError("split parent overlaps another directive")
                if by_id[parent_id]["parent_id"] is not None:
                    raise ValueError("split would exceed the two-level taxonomy")
                if not 3 <= len(children) <= 8:
                    raise ValueError("split requires 3 to 8 child topics")
                split_parents.add(parent_id)
                for child_id, topic in children:
                    canonical = normalize_interest_topic(topic)
                    if canonical != topic:
                        raise ValueError("split child topic is not normalized")
                    expected_child_id = "child-" + hashlib.sha256(
                        f"{parent_id}\0{canonical}".encode("utf-8")
                    ).hexdigest()[:24]
                    if child_id != expected_child_id:
                        raise ValueError("split child id is not deterministic")
                    if (
                        child_id in historical_ids or child_id in new_ids
                        or canonical in existing_topics or canonical in new_topics
                    ):
                        raise ValueError("split child id/topic conflicts with the graph")
                    new_ids.add(child_id)
                    new_topics.add(canonical)
                    child_total += 1

            half_ids = set(half_lives)
            if not half_ids.issubset(live_ids):
                raise ValueError("half-life references an unknown live interest")
            if half_ids & used_merge_ids:
                raise ValueError("half-life target overlaps a merge directive")
            for days in half_lives.values():
                if isinstance(days, bool):
                    raise ValueError("invalid interest half-life class")
                try:
                    numeric_days = float(days)
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid interest half-life class") from exc
                if not math.isfinite(numeric_days) or numeric_days not in INTEREST_HALF_LIFE_CLASSES:
                    raise ValueError("invalid interest half-life class")

            projected = len(rows) - len(absorbed) + child_total
            if child_total and projected > INTEREST_MAX_LIVE_TOPICS:
                raise ValueError("maintenance proposal would exceed the live-topic cap")

            for keep_id, absorb_id in merge_list:
                keep, absorb = by_id[keep_id], by_id[absorb_id]
                con.execute(
                    "UPDATE interest SET raw_score=?,evidence_count=?,last_evidence_at=?,"
                    "ts_alpha=?,ts_beta=?,updated_at=? WHERE interest_id=?",
                    (
                        float(keep["raw_score"]) + 0.5 * float(absorb["raw_score"]),
                        int(keep["evidence_count"]) + int(absorb["evidence_count"]),
                        max(float(keep["last_evidence_at"]), float(absorb["last_evidence_at"])),
                        float(keep["ts_alpha"]) + max(0.0, float(absorb["ts_alpha"]) - 1.0),
                        float(keep["ts_beta"]) + max(0.0, float(absorb["ts_beta"]) - 1.0),
                        timestamp, keep_id,
                    ),
                )
                con.execute(
                    "UPDATE interest SET state='retired',retired_at=?,updated_at=? WHERE interest_id=?",
                    (timestamp, timestamp, absorb_id),
                )
                # Preserve promotion provenance after synonym consolidation:
                # distinct evidence days for the absorbed topic now count toward
                # the canonical kept topic, while immutable source IDs remain.
                con.execute(
                    "UPDATE interest_event SET topic_text=? WHERE topic_text=?",
                    (str(keep["topic"]), str(absorb["topic"])),
                )
            for parent_id, children in split_list:
                parent = by_id[parent_id]
                for child_id, topic in children:
                    con.execute(
                        """INSERT INTO interest(
                          interest_id,topic,parent_id,raw_score,last_evidence_at,evidence_count,
                          valence,half_life_days,state,ts_alpha,ts_beta,created_at,updated_at,retired_at
                        ) VALUES(?,?,?,0,?,0,?,?,'candidate',1,1,?,?,NULL)""",
                        (
                            child_id, topic, parent_id, timestamp, parent["valence"],
                            float(parent["half_life_days"]), timestamp, timestamp,
                        ),
                    )
            for interest_id, days in half_lives.items():
                con.execute(
                    "UPDATE interest SET half_life_days=?,updated_at=? WHERE interest_id=?",
                    (float(days), timestamp, interest_id),
                )
            deterministic = self._apply_deterministic_interest_maintenance_in(
                con, timestamp=timestamp
            )
        return {
            "merged": [absorb for _, absorb in merge_list],
            "split_parents": [parent for parent, _ in split_list],
            **deterministic,
        }

    def apply_deterministic_interest_maintenance(
        self, *, now: float | None = None
    ) -> dict[str, list[str]]:
        """Promote, retire, and iteratively cap the taxonomy in one transaction."""
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            return self._apply_deterministic_interest_maintenance_in(
                con, timestamp=timestamp
            )

    def put_interest(self, interest: Interest) -> Interest:
        """Create or replace ledger source state; effective score remains derived."""
        canonical = normalize_interest_topic(interest.topic)
        if canonical != interest.topic:
            interest = replace(interest, topic=canonical)
        if interest.state is InterestState.RETIRED and interest.retired_at is None:
            raise ValueError("retired interests require retired_at")
        if interest.state is not InterestState.RETIRED and interest.retired_at is not None:
            raise ValueError("live interests cannot have retired_at")
        with self._immediate() as con:
            if interest.parent_id is not None:
                if interest.parent_id == interest.interest_id:
                    raise ValueError("an interest cannot be its own parent")
                parent = con.execute(
                    "SELECT parent_id,retired_at FROM interest WHERE interest_id=?",
                    (interest.parent_id,),
                ).fetchone()
                if parent is None or parent["parent_id"] is not None or parent["retired_at"] is not None:
                    raise ValueError("parent interest must be a live top-level interest")
                if con.execute(
                    "SELECT 1 FROM interest WHERE parent_id=? LIMIT 1",
                    (interest.interest_id,),
                ).fetchone() is not None:
                    raise ValueError("an interest with children must remain top-level")
            try:
                con.execute(
                    """INSERT INTO interest(
                  interest_id,topic,parent_id,raw_score,last_evidence_at,evidence_count,
                  valence,half_life_days,state,ts_alpha,ts_beta,created_at,updated_at,retired_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(interest_id) DO UPDATE SET
                  topic=excluded.topic,parent_id=excluded.parent_id,raw_score=excluded.raw_score,
                  last_evidence_at=excluded.last_evidence_at,evidence_count=excluded.evidence_count,
                  valence=excluded.valence,half_life_days=excluded.half_life_days,
                  state=excluded.state,ts_alpha=excluded.ts_alpha,ts_beta=excluded.ts_beta,
                  updated_at=excluded.updated_at,retired_at=excluded.retired_at""",
                    (
                        interest.interest_id, interest.topic, interest.parent_id,
                        interest.raw_score, interest.last_evidence_at, interest.evidence_count,
                        interest.valence.value, interest.half_life_days, interest.state.value,
                        interest.ts_alpha, interest.ts_beta, interest.created_at,
                        interest.updated_at, interest.retired_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed: interest.topic" in str(exc):
                    raise ValueError("topic already has a live interest") from exc
                raise
            row = con.execute(
                "SELECT * FROM interest WHERE interest_id=?", (interest.interest_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_interest(row)

    def get_interest(self, interest_id: str) -> Interest | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM interest WHERE interest_id=?", (interest_id,)
            ).fetchone()
        return self._row_to_interest(row) if row is not None else None

    def list_interests(
        self,
        *,
        state: InterestState | str | None = None,
        valence: InterestValence | str | None = None,
        parent_id: str | None = None,
        live_only: bool = False,
        min_effective_score: float | None = None,
        now: float | None = None,
    ) -> list[Interest]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state=?")
            params.append(InterestState(_enum_text(state)).value)
        if valence is not None:
            clauses.append("valence=?")
            params.append(InterestValence(_enum_text(valence)).value)
        if parent_id is not None:
            clauses.append("parent_id=?")
            params.append(parent_id)
        if live_only:
            clauses.append("retired_at IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM interest" + where + " ORDER BY updated_at DESC,interest_id",
                params,
            ).fetchall()
        interests = [self._row_to_interest(row) for row in rows]
        if min_effective_score is not None:
            timestamp = _finite_timestamp(now)
            threshold = float(min_effective_score)
            interests = [item for item in interests if item.effective_score(timestamp) >= threshold]
        return interests

    def eligible_interests(self, *, now: float | None = None, min_score: float = 2.0) -> list[Interest]:
        return self.list_interests(
            state=InterestState.ACTIVE, valence=InterestValence.POSITIVE,
            live_only=True, min_effective_score=min_score, now=now,
        )

    def update_interest_bandit(
        self, interest_id: str, *, alpha_delta: float = 0.0,
        beta_delta: float = 0.0, now: float | None = None,
    ) -> Interest:
        timestamp = _finite_timestamp(now)
        alpha_change = float(alpha_delta)
        beta_change = float(beta_delta)
        if not math.isfinite(alpha_change) or not math.isfinite(beta_change):
            raise ValueError("Thompson-sampling deltas must be finite")
        with self._immediate() as con:
            row = con.execute(
                "SELECT ts_alpha,ts_beta FROM interest WHERE interest_id=?", (interest_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown interest: {interest_id}")
            alpha = float(row["ts_alpha"]) + alpha_change
            beta = float(row["ts_beta"]) + beta_change
            if not math.isfinite(alpha) or not math.isfinite(beta) or alpha <= 0 or beta <= 0:
                raise ValueError("Thompson-sampling parameters must stay positive")
            con.execute(
                "UPDATE interest SET ts_alpha=?,ts_beta=?,updated_at=? WHERE interest_id=?",
                (alpha, beta, timestamp, interest_id),
            )
            updated = con.execute(
                "SELECT * FROM interest WHERE interest_id=?", (interest_id,)
            ).fetchone()
        assert updated is not None
        return self._row_to_interest(updated)

    def set_interest_half_life(
        self, interest_id: str, half_life_days: float, *, now: float | None = None
    ) -> Interest:
        """Apply a maintenance-assigned decay class; must stay strictly positive."""
        value = float(half_life_days)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("half_life_days must be positive and finite")
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            if con.execute(
                "SELECT 1 FROM interest WHERE interest_id=?", (interest_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown interest: {interest_id}")
            con.execute(
                "UPDATE interest SET half_life_days=?,updated_at=? WHERE interest_id=?",
                (value, timestamp, interest_id),
            )
            updated = con.execute(
                "SELECT * FROM interest WHERE interest_id=?", (interest_id,)
            ).fetchone()
        assert updated is not None
        return self._row_to_interest(updated)

    def set_interest_state(
        self, interest_id: str, state: InterestState | str, *, now: float | None = None
    ) -> Interest:
        """Move an interest between candidate/active/retired; sets retired_at atomically."""
        target = InterestState(_enum_text(state))
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            row = con.execute(
                "SELECT retired_at FROM interest WHERE interest_id=?", (interest_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown interest: {interest_id}")
            retired_at = timestamp if target is InterestState.RETIRED else None
            con.execute(
                "UPDATE interest SET state=?,retired_at=?,updated_at=? WHERE interest_id=?",
                (target.value, retired_at, timestamp, interest_id),
            )
            updated = con.execute(
                "SELECT * FROM interest WHERE interest_id=?", (interest_id,)
            ).fetchone()
        assert updated is not None
        return self._row_to_interest(updated)

    def merge_interests(
        self, keep_id: str, absorb_id: str, *, discount: float = 0.5, now: float | None = None
    ) -> Interest:
        """Fold ``absorb_id`` into ``keep_id`` with a discounted score transfer.

        The absorbed interest is retired (never deleted, preserving its event
        provenance). Score transfers at ``discount`` (plan: 0.5). Both interests
        must be live and share valence polarity; the caller (validator) enforces
        the cross-polarity ban before reaching here, but this is a second guard.
        """
        transfer_discount = float(discount)
        if not 0.0 <= transfer_discount <= 1.0:
            raise ValueError("discount must be in [0, 1]")
        if keep_id == absorb_id:
            raise ValueError("cannot merge an interest into itself")
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            keep = con.execute(
                "SELECT * FROM interest WHERE interest_id=?", (keep_id,)
            ).fetchone()
            absorb = con.execute(
                "SELECT * FROM interest WHERE interest_id=?", (absorb_id,)
            ).fetchone()
            if keep is None or absorb is None:
                raise KeyError("both interests must exist to merge")
            if keep["retired_at"] is not None or absorb["retired_at"] is not None:
                raise ValueError("cannot merge a retired interest")
            if keep["valence"] != absorb["valence"]:
                raise ValueError("cannot merge across valence polarity")
            if con.execute(
                "SELECT 1 FROM interest WHERE parent_id=? LIMIT 1", (absorb_id,)
            ).fetchone() is not None:
                raise ValueError("cannot merge an interest that still has children")
            new_score = float(keep["raw_score"]) + transfer_discount * float(absorb["raw_score"])
            new_count = int(keep["evidence_count"]) + int(absorb["evidence_count"])
            new_last = max(float(keep["last_evidence_at"]), float(absorb["last_evidence_at"]))
            new_alpha = float(keep["ts_alpha"]) + max(0.0, float(absorb["ts_alpha"]) - 1.0)
            new_beta = float(keep["ts_beta"]) + max(0.0, float(absorb["ts_beta"]) - 1.0)
            con.execute(
                "UPDATE interest SET raw_score=?,evidence_count=?,last_evidence_at=?,"
                "ts_alpha=?,ts_beta=?,updated_at=? WHERE interest_id=?",
                (new_score, new_count, new_last, new_alpha, new_beta, timestamp, keep_id),
            )
            con.execute(
                "UPDATE interest SET state='retired',retired_at=?,updated_at=? WHERE interest_id=?",
                (timestamp, timestamp, absorb_id),
            )
            merged = con.execute(
                "SELECT * FROM interest WHERE interest_id=?", (keep_id,)
            ).fetchone()
        assert merged is not None
        return self._row_to_interest(merged)

    def record_proactive_send(self, send: ProactiveSend) -> ProactiveSend:
        if not send.send_id.strip() or not send.gate_reason.strip():
            raise ValueError("send_id and gate_reason are required")
        # This is the hard context boundary for proactive fetch output.  Check-in
        # payloads and suppressions use the same bounded object contract so no
        # alternate write path can persist a delegate's raw research dump.
        if len(send.candidate_json) > 500:
            raise ValueError("candidate_json cannot exceed 500 characters")
        try:
            candidate = json.loads(send.candidate_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("candidate_json must be valid JSON") from exc
        if not isinstance(candidate, dict):
            raise ValueError("candidate_json must contain an object")
        item_hash = self._candidate_item_hash(send.candidate_json)
        if send.gate_decision is GateDecision.SENT and send.sent_at is None:
            raise ValueError("sent proactive records require sent_at")
        if send.gate_decision is GateDecision.SUPPRESSED and send.sent_at is not None:
            raise ValueError("suppressed proactive records cannot have sent_at")
        if send.gate_decision is GateDecision.SUPPRESSED and send.outcome is not None:
            raise ValueError("suppressed proactive records cannot have outcomes")
        if send.outcome is None and send.outcome_at is not None:
            raise ValueError("outcome_at requires an outcome")
        if send.outcome is not None and send.outcome_at is None:
            raise ValueError("outcomes require outcome_at")
        if send.outcome_at is not None and send.sent_at is not None and send.outcome_at < send.sent_at:
            raise ValueError("outcome_at cannot precede sent_at")
        with self._immediate() as con:
            if send.interest_id is not None and con.execute(
                "SELECT 1 FROM interest WHERE interest_id=?", (send.interest_id,)
            ).fetchone() is None:
                raise ValueError("proactive send references an unknown interest")
            con.execute(
                """INSERT OR IGNORE INTO proactive_send(
                  send_id,interest_id,kind,candidate_json,item_hash,gate_decision,gate_reason,
                  sent_at,outcome,outcome_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    send.send_id, send.interest_id, send.kind.value, send.candidate_json, item_hash,
                    send.gate_decision.value, send.gate_reason, send.sent_at,
                    send.outcome.value if send.outcome is not None else None,
                    send.outcome_at, send.created_at,
                ),
            )
            row = con.execute(
                "SELECT * FROM proactive_send WHERE send_id=?", (send.send_id,)
            ).fetchone()
        assert row is not None
        stored = self._row_to_proactive_send(row)
        if stored != send:
            raise ValueError("send_id already belongs to a different proactive send")
        return stored

    def get_proactive_send(self, send_id: str) -> ProactiveSend | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM proactive_send WHERE send_id=?", (send_id,)
            ).fetchone()
        return self._row_to_proactive_send(row) if row is not None else None

    def has_proactive_item_hash(self, item_hash: str) -> bool:
        """Check sent and suppressed novelty history through the durable index."""
        value = str(item_hash).strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("item_hash must be a SHA-256 hex digest")
        with self._connect() as con:
            return con.execute(
                "SELECT 1 FROM proactive_send WHERE item_hash=? LIMIT 1", (value,)
            ).fetchone() is not None

    def recent_proactive_sends(
        self, *, since: float = 0.0, decision: GateDecision | str | None = None,
        limit: int = 100,
    ) -> list[ProactiveSend]:
        clauses = ["created_at>=?"]
        params: list[object] = [float(since)]
        if decision is not None:
            clauses.append("gate_decision=?")
            params.append(GateDecision(_enum_text(decision)).value)
        params.append(max(0, int(limit)))
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM proactive_send WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC,send_id LIMIT ?", params,
            ).fetchall()
        return [self._row_to_proactive_send(row) for row in rows]

    def set_proactive_send_outcome(
        self, send_id: str, outcome: ProactiveOutcome | str, *, now: float | None = None
    ) -> ProactiveSend:
        """Set an outcome without ledger feedback (legacy/admin helper).

        Runtime outcome tracking should use :meth:`record_proactive_outcome`,
        which closes the proactive-send and bandit/event loop atomically.
        """
        resolved = ProactiveOutcome(_enum_text(outcome))
        timestamp = _finite_timestamp(now)
        with self._immediate() as con:
            row = con.execute(
                "SELECT gate_decision,sent_at,outcome FROM proactive_send WHERE send_id=?", (send_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown proactive send: {send_id}")
            if row["gate_decision"] != GateDecision.SENT.value or row["sent_at"] is None:
                raise ValueError("only sent proactive records can receive outcomes")
            if timestamp < float(row["sent_at"]):
                raise ValueError("outcome_at cannot precede sent_at")
            if row["outcome"] is not None and row["outcome"] != resolved.value:
                raise ValueError("proactive send already has a different outcome")
            con.execute(
                "UPDATE proactive_send SET outcome=?,outcome_at=COALESCE(outcome_at,?) WHERE send_id=?",
                (resolved.value, timestamp, send_id),
            )
            updated = con.execute(
                "SELECT * FROM proactive_send WHERE send_id=?", (send_id,)
            ).fetchone()
        assert updated is not None
        return self._row_to_proactive_send(updated)

    def record_proactive_outcome(
        self,
        send_id: str,
        outcome: ProactiveOutcome | str,
        *,
        source_id: str,
        now: float | None = None,
    ) -> ProactiveSend:
        """Atomically close a send and apply exactly-once interest feedback.

        The deterministic event id is inserted with ``folded_at`` already set,
        because this transaction applies its score and Thompson deltas directly.
        A retry can therefore repair a historical outcome row that lacks its
        event, but can never double-update the bandit or raw score.
        """
        from .schema import INTEREST_SIGNAL_BANDIT, INTEREST_SIGNAL_WEIGHTS

        resolved = ProactiveOutcome(_enum_text(outcome))
        timestamp = _finite_timestamp(now)
        source = str(source_id).strip()
        if not source or len(source) > 500 or any(ord(ch) < 32 for ch in source):
            raise ValueError("source_id is invalid")
        feedback = {
            ProactiveOutcome.ENGAGED: (
                SignalType.PROACTIVE_ENGAGED, InterestValence.POSITIVE,
            ),
            ProactiveOutcome.ACKNOWLEDGED: (
                SignalType.NEUTRAL_ACK, InterestValence.NEUTRAL,
            ),
            ProactiveOutcome.IGNORED: (
                SignalType.PROACTIVE_IGNORED, InterestValence.NEUTRAL,
            ),
            ProactiveOutcome.DISMISSED: (
                SignalType.DISMISSIVE, InterestValence.NEUTRAL,
            ),
        }
        with self._immediate() as con:
            row = con.execute(
                "SELECT * FROM proactive_send WHERE send_id=?", (send_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown proactive send: {send_id}")
            if row["gate_decision"] != GateDecision.SENT.value or row["sent_at"] is None:
                raise ValueError("only sent proactive records can receive outcomes")
            if timestamp < float(row["sent_at"]):
                raise ValueError("outcome_at cannot precede sent_at")
            if row["outcome"] is not None and row["outcome"] != resolved.value:
                raise ValueError("proactive send already has a different outcome")
            con.execute(
                "UPDATE proactive_send SET outcome=?,outcome_at=COALESCE(outcome_at,?) "
                "WHERE send_id=?",
                (resolved.value, timestamp, send_id),
            )
            interest_id = row["interest_id"]
            if interest_id is not None:
                interest = con.execute(
                    "SELECT topic FROM interest WHERE interest_id=?", (interest_id,)
                ).fetchone()
                if interest is None:
                    raise RuntimeError("proactive send references a missing interest")
                signal, valence = feedback[resolved]
                event_id = hashlib.sha256(
                    f"proactive-outcome\0{send_id}\0{resolved.value}".encode()
                ).hexdigest()
                inserted = con.execute(
                    """INSERT OR IGNORE INTO interest_event(
                       event_id,topic_text,signal_type,valence,source_id,created_at,folded_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        event_id, str(interest["topic"]), signal.value, valence.value,
                        source, timestamp, timestamp,
                    ),
                ).rowcount
                if inserted:
                    alpha_delta, beta_delta = INTEREST_SIGNAL_BANDIT.get(
                        signal, (0.0, 0.0)
                    )
                    con.execute(
                        """UPDATE interest SET
                           raw_score=raw_score+?,
                           evidence_count=evidence_count+1,
                           last_evidence_at=MAX(last_evidence_at,?),
                           ts_alpha=ts_alpha+?,ts_beta=ts_beta+?,updated_at=?
                           WHERE interest_id=?""",
                        (
                            INTEREST_SIGNAL_WEIGHTS.get(signal, 0.0), timestamp,
                            alpha_delta, beta_delta, timestamp, interest_id,
                        ),
                    )
            updated = con.execute(
                "SELECT * FROM proactive_send WHERE send_id=?", (send_id,)
            ).fetchone()
        assert updated is not None
        return self._row_to_proactive_send(updated)

    @staticmethod
    def _decode_message_length_samples(value: str | None) -> list[dict[str, Any]]:
        try:
            payload = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        samples: list[dict[str, Any]] = []
        for item in payload[-_MESSAGE_LENGTH_WINDOW:]:
            if not isinstance(item, dict):
                continue
            try:
                length = int(item["length"])
                identity = str(item["id"])
                median = item.get("baseline_median")
                is_long = bool(item["is_long"])
                sample_count = int(item["sample_count"])
            except (KeyError, TypeError, ValueError):
                continue
            if identity and length >= 0 and (median is None or isinstance(median, (int, float))):
                samples.append({
                    "id": identity, "length": length,
                    "baseline_median": float(median) if median is not None else None,
                    "is_long": is_long, "sample_count": sample_count,
                })
        return samples

    def record_contact_message_length(
        self, source_id: str, message_length: int
    ) -> MessageLengthObservation:
        """Classify against the prior rolling median and persist only lengths."""
        source = str(source_id).strip()
        if not source:
            raise ValueError("source_id is required")
        length = int(message_length)
        if length < 0:
            raise ValueError("message_length cannot be negative")
        identity = hashlib.sha256(source.encode()).hexdigest()
        with self._immediate() as con:
            row = con.execute(
                "SELECT value FROM schema_meta WHERE key=?", (_MESSAGE_LENGTH_META_KEY,)
            ).fetchone()
            samples = self._decode_message_length_samples(row[0] if row else None)
            for sample in samples:
                if sample["id"] == identity:
                    return MessageLengthObservation(
                        message_length=int(sample["length"]),
                        baseline_median=sample["baseline_median"],  # type: ignore[arg-type]
                        is_long_reply=bool(sample["is_long"]),
                        sample_count=int(sample["sample_count"]),
                    )
            prior_lengths = [int(sample["length"]) for sample in samples]
            baseline = float(statistics.median(prior_lengths)) if prior_lengths else None
            is_long = baseline is not None and length > 2.0 * baseline
            observation = MessageLengthObservation(
                message_length=length, baseline_median=baseline,
                is_long_reply=is_long, sample_count=len(samples) + 1,
            )
            samples.append({
                "id": identity, "length": length, "baseline_median": baseline,
                "is_long": is_long, "sample_count": observation.sample_count,
            })
            samples = samples[-_MESSAGE_LENGTH_WINDOW:]
            con.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_MESSAGE_LENGTH_META_KEY, json.dumps(samples, separators=(",", ":"))),
            )
        return observation

    def contact_message_length_baseline(self) -> MessageLengthBaseline:
        with self._connect() as con:
            row = con.execute(
                "SELECT value FROM schema_meta WHERE key=?", (_MESSAGE_LENGTH_META_KEY,)
            ).fetchone()
        samples = self._decode_message_length_samples(row[0] if row else None)
        lengths = [int(sample["length"]) for sample in samples]
        return MessageLengthBaseline(
            sample_count=len(lengths),
            median=float(statistics.median(lengths)) if lengths else None,
        )

    def secure_delete_all(self) -> None:
        with self._immediate() as con:
            for table in (
                "communication_recommendation_event", "entity_mention",
                "communication_retraction_pending", "communication_relation",
                "communication_attachment", "communication_url",
                "communication_event", "proactive_send", "interest", "interest_event", "callback_event",
                "recall_event", "embedding", "edge", "recommendation", "pending_fact", "fact",
            ):
                con.execute(f"DELETE FROM {table}")
            con.execute("DELETE FROM schema_meta WHERE key=?", (_MESSAGE_LENGTH_META_KEY,))
        with self._connect() as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")

    def export_owner(self) -> dict[str, object]:
        facts = self.active_facts(RetrievalPrincipal.OWNER)
        return {"contact_namespace": opaque_contact_filename(self.contact_id).removesuffix(".sqlite3"), "facts": [fact.__dict__ for fact in facts]}
