"""Strict data contracts and SQLite schema for contact memory.

Facts are immutable versions. Updating a logical fact closes the old transaction
and valid-time interval and inserts a replacement in one immediate transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


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
  status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
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
  created_at REAL NOT NULL
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
