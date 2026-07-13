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
    AssertionType,
    Audience,
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
    r"\b(?:abortion|abuse|addiction|affair|allerg(?:y|ies)|anxiety|arrest|bank|"
    r"assault|bereavement|cancer|contraception|crime|death|debt|depress\w*|"
    r"diabet\w*|diagnos\w*|disease|disorder|divorc\w*|domestic|doctor|dying|"
    r"fetish\w*|funeral|grief|harm|health|hiv|hospital|illness|income|"
    r"immigration|lawsuit|lawyer|legal|medical|medication\w*|mental|money|opioid|"
    r"overdose|passport|password|pregnan\w*|prescription|rape|rehab|salary|secret|"
    r"sex(?:ual)?|ssn|std|sti|suicid\w*|surgery|tax(?:es)?|therapy|token|trauma|"
    r"violence|visa)\b",
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
            if existing is not None and str(existing[0]) not in {"1", "2", str(SCHEMA_VERSION)}:
                raise RuntimeError(
                    f"unsupported contact-memory schema {existing[0]}; expected {SCHEMA_VERSION}"
                )
            if existing is not None and str(existing[0]) in {"1", "2"}:
                self._upgrade_to_current(con)
                return
            con.executescript(CONTACT_SCHEMA_SQL)
            con.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _upgrade_to_current(self, con: sqlite3.Connection) -> None:
        """Atomically migrate a supported historical contact database to v3."""
        con.execute("BEGIN IMMEDIATE")
        try:
            current = con.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            from_version = str(current[0]) if current is not None else ""
            if from_version == str(SCHEMA_VERSION):
                con.execute("COMMIT")
                return
            if from_version not in {"1", "2"}:
                raise RuntimeError(
                    f"unsupported contact-memory schema {from_version}; expected {SCHEMA_VERSION}"
                )
            if from_version == "1":
                self._migrate_v1_to_v2(con)
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

    def record_proactive_send(self, send: ProactiveSend) -> ProactiveSend:
        if not send.send_id.strip() or not send.gate_reason.strip():
            raise ValueError("send_id and gate_reason are required")
        try:
            candidate = json.loads(send.candidate_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("candidate_json must be valid JSON") from exc
        if not isinstance(candidate, dict):
            raise ValueError("candidate_json must contain an object")
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
                  send_id,interest_id,kind,candidate_json,gate_decision,gate_reason,
                  sent_at,outcome,outcome_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    send.send_id, send.interest_id, send.kind.value, send.candidate_json,
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
                "proactive_send", "interest", "interest_event", "callback_event",
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
