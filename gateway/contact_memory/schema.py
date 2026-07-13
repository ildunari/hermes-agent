"""Strict data contracts and SQLite schema for contact memory.

Facts are immutable versions. Updating a logical fact closes the old transaction
and valid-time interval and inserts a replacement in one immediate transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any

SCHEMA_VERSION = 3


class StrEnum(str, Enum):
    pass


class RetrievalPrincipal(StrEnum):
    OWNER = "owner"
    GUEST = "guest"


class Audience(StrEnum):
    OWNER_ONLY = "owner_only"
    OWNER_REVIEW = "owner_review"
    GUEST_OK = "guest_ok"
    PUBLIC = "public"


class MentionPolicy(StrEnum):
    BACKGROUND = "background"
    MENTIONABLE = "mentionable"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class AssertionType(StrEnum):
    STATED = "stated"
    OBSERVED = "observed"
    INFERRED = "inferred"


class FactStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"
    QUARANTINED = "quarantined"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    REJECTED = "rejected"


class RecommendationStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
    FULFILLED = "fulfilled"
    REJECTED = "rejected"


class SignalType(StrEnum):
    SPONTANEOUS_RAISE = "spontaneous_raise"
    ENTHUSIASM = "enthusiasm"
    LONG_REPLY = "long_reply"
    ENGAGED_MENTION = "engaged_mention"
    NEUTRAL_ACK = "neutral_ack"
    DISMISSIVE = "dismissive"
    EXPLICIT_NEGATIVE = "explicit_negative"
    PROACTIVE_ENGAGED = "proactive_engaged"
    PROACTIVE_IGNORED = "proactive_ignored"


class InterestValence(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class InterestState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    RETIRED = "retired"


class ProactiveSendKind(StrEnum):
    INTEREST_SHARE = "interest_share"
    CHECKIN = "checkin"
    EXPLORATION = "exploration"


class GateDecision(StrEnum):
    SENT = "sent"
    SUPPRESSED = "suppressed"


class ProactiveOutcome(StrEnum):
    ENGAGED = "engaged"
    ACKNOWLEDGED = "acknowledged"
    IGNORED = "ignored"
    DISMISSED = "dismissed"


# Deterministic fold weights (plan §"Signal weights"). These are the single
# source of truth for both the store fold and the maintenance module; changing a
# number here changes ledger scoring everywhere. ``ts_*`` deltas feed the
# Thompson-sampling bandit that Phase 3 reads.
INTEREST_SIGNAL_WEIGHTS: dict[SignalType, float] = {
    SignalType.SPONTANEOUS_RAISE: 1.0,
    SignalType.ENTHUSIASM: 0.8,
    SignalType.LONG_REPLY: 0.6,
    SignalType.ENGAGED_MENTION: 0.4,
    SignalType.NEUTRAL_ACK: 0.1,
    SignalType.PROACTIVE_ENGAGED: 1.2,
    SignalType.PROACTIVE_IGNORED: -0.3,
    SignalType.DISMISSIVE: -0.8,
    SignalType.EXPLICIT_NEGATIVE: -2.0,
}
INTEREST_SIGNAL_BANDIT: dict[SignalType, tuple[float, float]] = {
    SignalType.PROACTIVE_ENGAGED: (1.0, 0.0),
    SignalType.PROACTIVE_IGNORED: (0.0, 1.0),
    SignalType.DISMISSIVE: (0.0, 1.0),
}

# Promotion / retirement thresholds (plan §"Signal weights", promotion rules).
INTEREST_PROMOTE_MIN_SCORE = 1.5
INTEREST_PROMOTE_MIN_DISTINCT_DAYS = 2
INTEREST_RETIRE_MAX_SCORE = 0.2
INTEREST_ELIGIBLE_MIN_SCORE = 2.0

# Half-life classes the maintenance model may assign (transient / hobby / identity).
INTEREST_HALF_LIFE_CLASSES = frozenset({14.0, 90.0, 365.0})

# Taxonomy caps enforced by the maintenance validator.
INTEREST_MAX_LIVE_TOPICS = 40
INTEREST_MAX_TAXONOMY_DEPTH = 2


@dataclass(frozen=True)
class InterestEvent:
    event_id: str
    topic_text: str
    signal_type: SignalType
    valence: InterestValence
    source_id: str
    created_at: float
    folded_at: float | None = None

    def __post_init__(self) -> None:
        for name in ("event_id", "topic_text", "source_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if not isinstance(self.signal_type, SignalType):
            raise ValueError("signal_type is invalid")
        if not isinstance(self.valence, InterestValence):
            raise ValueError("valence is invalid")
        if not math.isfinite(float(self.created_at)):
            raise ValueError("created_at must be finite")
        if self.folded_at is not None and not math.isfinite(float(self.folded_at)):
            raise ValueError("folded_at must be finite")


@dataclass(frozen=True)
class Interest:
    interest_id: str
    topic: str
    parent_id: str | None
    raw_score: float
    last_evidence_at: float
    evidence_count: int
    valence: InterestValence
    half_life_days: float
    state: InterestState
    ts_alpha: float
    ts_beta: float
    created_at: float
    updated_at: float
    retired_at: float | None

    def __post_init__(self) -> None:
        for name in ("interest_id", "topic"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        for name in (
            "raw_score", "last_evidence_at", "half_life_days", "ts_alpha",
            "ts_beta", "created_at", "updated_at",
        ):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if self.half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        if self.evidence_count < 0:
            raise ValueError("evidence_count cannot be negative")
        if self.ts_alpha <= 0 or self.ts_beta <= 0:
            raise ValueError("Thompson-sampling parameters must be positive")
        if not isinstance(self.valence, InterestValence):
            raise ValueError("valence is invalid")
        if not isinstance(self.state, InterestState):
            raise ValueError("state is invalid")
        if self.retired_at is not None and not math.isfinite(float(self.retired_at)):
            raise ValueError("retired_at must be finite")

    def effective_score(self, now: float) -> float:
        """Return the decayed score without persisting derived state."""
        return self.raw_score * 2.0 ** (
            -(float(now) - self.last_evidence_at) / 86_400.0 / self.half_life_days
        )


@dataclass(frozen=True)
class ProactiveSend:
    send_id: str
    interest_id: str | None
    kind: ProactiveSendKind
    candidate_json: str
    gate_decision: GateDecision
    gate_reason: str
    sent_at: float | None
    outcome: ProactiveOutcome | None
    outcome_at: float | None
    created_at: float

    def __post_init__(self) -> None:
        for name in ("send_id", "candidate_json", "gate_reason"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if not isinstance(self.kind, ProactiveSendKind):
            raise ValueError("kind is invalid")
        if not isinstance(self.gate_decision, GateDecision):
            raise ValueError("gate_decision is invalid")
        if self.outcome is not None and not isinstance(self.outcome, ProactiveOutcome):
            raise ValueError("outcome is invalid")
        for name in ("sent_at", "outcome_at"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if not math.isfinite(float(self.created_at)):
            raise ValueError("created_at must be finite")


@dataclass(frozen=True)
class FactProposal:
    logical_id: str
    subject_id: str
    predicate: str
    object_text: str
    audience: Audience = Audience.OWNER_ONLY
    mention_policy: MentionPolicy = MentionPolicy.BACKGROUND
    assertion_type: AssertionType = AssertionType.STATED
    source_id: str = ""
    source_contact_id: str = ""
    evidence_pointer: str = ""
    trust: float = 0.5
    confidence: float = 0.5
    status: FactStatus = FactStatus.ACTIVE
    valid_from: float | None = None
    valid_to: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("logical_id", "subject_id", "predicate", "object_text", "source_id", "source_contact_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        for name in ("trust", "confidence"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        for name in ("valid_from", "valid_to"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.valid_from is not None and self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be greater than valid_from")


@dataclass(frozen=True)
class FactRecord(FactProposal):
    version_id: str = ""
    tx_from: float = 0.0
    tx_to: float | None = None
    created_at: float = 0.0


@dataclass(frozen=True)
class SearchResult:
    fact: FactRecord
    score: float
    lexical_score: float = 0.0
    semantic_score: float = 0.0


CONTACT_SCHEMA_SQL = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact (
  version_id TEXT PRIMARY KEY,
  logical_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_text TEXT NOT NULL,
  audience TEXT NOT NULL CHECK(audience IN ('owner_only','owner_review','guest_ok','public')),
  mention_policy TEXT NOT NULL CHECK(mention_policy IN ('background','mentionable','sensitive','restricted')),
  assertion_type TEXT NOT NULL CHECK(assertion_type IN ('stated','observed','inferred')),
  source_id TEXT NOT NULL,
  source_contact_id TEXT NOT NULL,
  evidence_pointer TEXT NOT NULL DEFAULT '',
  trust REAL NOT NULL CHECK(trust >= 0 AND trust <= 1),
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  status TEXT NOT NULL CHECK(status IN ('active','pending','quarantined','superseded','withdrawn','rejected')),
  valid_from REAL NOT NULL,
  valid_to REAL,
  tx_from REAL NOT NULL,
  tx_to REAL,
  created_at REAL NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  CHECK(valid_to IS NULL OR valid_to > valid_from),
  CHECK(tx_to IS NULL OR tx_to >= tx_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_fact
  ON fact(logical_id) WHERE status='active' AND tx_to IS NULL;
CREATE INDEX IF NOT EXISTS fact_visibility
  ON fact(status, audience, mention_policy, assertion_type, trust, confidence);
CREATE TABLE IF NOT EXISTS embedding (
  version_id TEXT NOT NULL REFERENCES fact(version_id) ON DELETE CASCADE,
  model_id TEXT NOT NULL,
  dimensions INTEGER NOT NULL CHECK(dimensions > 0),
  vector_le_f32 BLOB NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(version_id, model_id)
);
CREATE TABLE IF NOT EXISTS edge (
  edge_version_id TEXT PRIMARY KEY,
  logical_id TEXT NOT NULL,
  source_entity_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  target_entity_id TEXT NOT NULL,
  audience TEXT NOT NULL,
  mention_policy TEXT NOT NULL,
  assertion_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_contact_id TEXT NOT NULL,
  evidence_pointer TEXT NOT NULL DEFAULT '',
  trust REAL NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL,
  valid_from REAL NOT NULL,
  valid_to REAL,
  tx_from REAL NOT NULL,
  tx_to REAL,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_edge
  ON edge(logical_id) WHERE status='active' AND tx_to IS NULL;
CREATE TABLE IF NOT EXISTS pending_fact (
  proposal_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_contact_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','superseded','promoted')),
  created_at REAL NOT NULL,
  decided_at REAL
);
CREATE TABLE IF NOT EXISTS recommendation (
  recommendation_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  recommendation TEXT NOT NULL,
  basis_fact_ids_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('proposed','active','withdrawn','fulfilled','rejected')),
  supersedes_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL,
  expires_at REAL,
  change_requirements_json TEXT NOT NULL DEFAULT '[]',
  idempotency_key TEXT UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_recommendation
  ON recommendation(topic) WHERE status='active';
CREATE TABLE IF NOT EXISTS recall_event (
  event_id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  turn_index INTEGER NOT NULL,
  fact_version_id TEXT NOT NULL REFERENCES fact(version_id),
  event_type TEXT NOT NULL CHECK(event_type IN ('retrieved','used')),
  created_at REAL NOT NULL,
  UNIQUE(session_key, turn_index, fact_version_id, event_type)
);
CREATE INDEX IF NOT EXISTS recall_cooldown
  ON recall_event(session_key, event_type, created_at, turn_index);
CREATE TABLE IF NOT EXISTS callback_event (
  event_id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK(subject_type IN ('fact','recommendation')),
  subject_id TEXT NOT NULL,
  turn_index INTEGER NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(session_key, subject_type, subject_id, turn_index)
);
CREATE INDEX IF NOT EXISTS callback_event_cooldown
  ON callback_event(session_key, subject_type, subject_id, created_at, turn_index);
CREATE TABLE IF NOT EXISTS interest_event (
  event_id TEXT PRIMARY KEY,
  topic_text TEXT NOT NULL,
  signal_type TEXT NOT NULL CHECK(signal_type IN (
    'spontaneous_raise','enthusiasm','long_reply','engaged_mention',
    'neutral_ack','dismissive','explicit_negative','proactive_engaged','proactive_ignored')),
  valence TEXT NOT NULL CHECK(valence IN ('positive','negative','neutral')),
  source_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  folded_at REAL
);
CREATE INDEX IF NOT EXISTS interest_event_unfolded ON interest_event(folded_at, created_at);
CREATE TABLE IF NOT EXISTS interest (
  interest_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  parent_id TEXT REFERENCES interest(interest_id),
  raw_score REAL NOT NULL DEFAULT 0,
  last_evidence_at REAL NOT NULL,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  valence TEXT NOT NULL DEFAULT 'positive' CHECK(valence IN ('positive','negative','neutral')),
  half_life_days REAL NOT NULL DEFAULT 90,
  state TEXT NOT NULL CHECK(state IN ('candidate','active','retired')) DEFAULT 'candidate',
  ts_alpha REAL NOT NULL DEFAULT 1,
  ts_beta REAL NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  retired_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS interest_topic_live ON interest(topic) WHERE retired_at IS NULL;
CREATE TABLE IF NOT EXISTS proactive_send (
  send_id TEXT PRIMARY KEY,
  interest_id TEXT REFERENCES interest(interest_id),
  kind TEXT NOT NULL CHECK(kind IN ('interest_share','checkin','exploration')),
  candidate_json TEXT NOT NULL,
  gate_decision TEXT NOT NULL CHECK(gate_decision IN ('sent','suppressed')),
  gate_reason TEXT NOT NULL,
  sent_at REAL,
  outcome TEXT CHECK(outcome IN ('engaged','acknowledged','ignored','dismissed')),
  outcome_at REAL,
  created_at REAL NOT NULL,
  CHECK((gate_decision='sent' AND sent_at IS NOT NULL) OR
        (gate_decision='suppressed' AND sent_at IS NULL AND outcome IS NULL AND outcome_at IS NULL)),
  CHECK((outcome IS NULL AND outcome_at IS NULL) OR
        (outcome IS NOT NULL AND outcome_at IS NOT NULL AND
         sent_at IS NOT NULL AND outcome_at >= sent_at))
);
CREATE INDEX IF NOT EXISTS proactive_send_recent
  ON proactive_send(created_at DESC, gate_decision);
"""

REGISTRY_SCHEMA_SQL = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entity (
  entity_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK(entity_type IN ('person','place','organization','thing','event')),
  canonical_label TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
"""
