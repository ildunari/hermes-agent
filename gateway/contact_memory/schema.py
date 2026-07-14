"""Strict data contracts and SQLite schema for contact memory.

Facts are immutable versions. Updating a logical fact closes the old transaction
and valid-time interval and inserts a replacement in one immediate transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import math
import re
from typing import Any

SCHEMA_VERSION = 8

_PROACTIVE_ITEM_WORD_RE = re.compile(r"[a-z0-9]+", re.I)


def normalized_proactive_item_hash(concrete_item: str) -> str:
    """Return the stable, formatting-insensitive identity for a shared item."""
    words = [word.casefold() for word in _PROACTIVE_ITEM_WORD_RE.findall(str(concrete_item))]
    normalized = " ".join(
        word[:-1] if len(word) > 3 and word.endswith("s") else word for word in words
    )
    return hashlib.sha256(("proactive-item-v1\0" + normalized).encode("utf-8")).hexdigest()


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


class CommunicationDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CommunicationKind(StrEnum):
    TEXT = "text"
    LINK_SHARE = "link_share"
    ATTACHMENT_SHARE = "attachment_share"
    REACTION_ADD = "reaction_add"
    REACTION_REMOVE = "reaction_remove"
    REPLY = "reply"
    BATCH_MEMBER = "batch_member"
    RECOMMENDATION = "recommendation"
    FOLLOW_THROUGH = "follow_through"
    CALLBACK = "callback"


class CommunicationReactionSubtype(StrEnum):
    LIKE = "like"
    LOVE = "love"
    DISLIKE = "dislike"
    LAUGH = "laugh"
    EMPHASIS = "emphasis"
    QUESTION = "question"
    LEGACY_UNTYPED = "legacy_untyped"


class CommunicationActorRole(StrEnum):
    CONTACT = "contact"
    COUNTERPART = "counterpart"
    ASSISTANT = "assistant"


class CommunicationPrivacy(StrEnum):
    PRIVATE = "private"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class CommunicationLifecycle(StrEnum):
    ACTIVE = "active"
    RETRACTED = "retracted"


class CommunicationRelationType(StrEnum):
    REPLY_TO = "reply_to"
    REACTION_TO = "reaction_to"
    BATCH_MEMBER_OF = "batch_member_of"
    RECOMMENDS = "recommends"
    FOLLOWS_THROUGH = "follows_through"
    CALLBACK_TO = "callback_to"


class CommunicationEnrichmentState(StrEnum):
    PENDING = "pending"
    REVIEWED = "reviewed"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class CommunicationRecommendationOutcome(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVISITED = "revisited"
    FULFILLED = "fulfilled"


class ProjectionMethod(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


_OPAQUE_ID_RE = re.compile(r"[0-9a-f]{64}")


def _require_opaque_id(value: object, *, name: str) -> str:
    normalized = str(value or "")
    if not _OPAQUE_ID_RE.fullmatch(normalized):
        raise ValueError(f"{name} must be a 64-character lowercase hex identity")
    return normalized


def _require_short_text(value: object, *, name: str, maximum: int) -> str:
    normalized = str(value or "")
    if (
        not normalized
        or normalized != normalized.strip()
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError(f"{name} must contain 1-{maximum} characters")
    return normalized


def _require_recommendation_id(value: object) -> str:
    normalized = str(value or "")
    if not re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{64})", normalized):
        raise ValueError("recommendation_id must be a lowercase opaque identity")
    return normalized


def _reject_raw_artifact_text(value: str, *, name: str) -> None:
    lowered = value.casefold()
    if (
        "://" in lowered or lowered.startswith(("file:", "/", "~"))
        or "/" in value or "\\" in value
        or "www." in lowered
        or re.search(r"(?:^|\s)(?:~?/|[a-z]:\\)\S+", value, re.IGNORECASE)
        or re.search(r"(?:^|[?&])(token|key|signature|auth)=", lowered)
    ):
        raise ValueError(f"{name} cannot contain a raw URL, secret, or local path")


_ENTITY_CREDENTIAL_NAME_PATTERN = (
    r"(?:password|passcode|pin|token|api[ -]?(?:key|secret)|client secret|access token|"
    r"auth token|bearer token|private key|(?:secret )?access key|credential(?:s)?|seed phrase)"
)
_ENTITY_CREDENTIAL_MARKER_RE = re.compile(
    rf"\b{_ENTITY_CREDENTIAL_NAME_PATTERN}\b", re.IGNORECASE
)
_ENTITY_CREDENTIAL_VALUE_RE = re.compile(
    rf"(?<!\w)(?:[a-z0-9][\w.-]*['’]s\s+)?{_ENTITY_CREDENTIAL_NAME_PATTERN}"
    r"(?:(?:\s+(?:is|are)\s+)|\s*[:=]\s*)\S+|"
    rf"\b{_ENTITY_CREDENTIAL_NAME_PATTERN}\s+(?!is\b|are\b)\S*[0-9_.=-]\S*|"
    r"\bseed phrase(?:\s+\S+){3,}|\bsk-[A-Za-z0-9_-]{8,}\b",
    re.IGNORECASE,
)


def _reject_credential_like_entity_label(value: str) -> None:
    markers = _ENTITY_CREDENTIAL_MARKER_RE.findall(value)
    if len(markers) >= 2 or _ENTITY_CREDENTIAL_VALUE_RE.search(value):
        raise ValueError("canonical_label cannot contain credential-like message text")


def _require_machine_token(value: str, *, name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(f"{name} must be a normalized machine token")


@dataclass(frozen=True)
class CommunicationEvent:
    event_id: str
    platform: str
    source_id: str
    occurred_at: float
    direction: CommunicationDirection
    kind: CommunicationKind
    actor_role: CommunicationActorRole
    reaction_subtype: CommunicationReactionSubtype | None = None
    privacy: CommunicationPrivacy = CommunicationPrivacy.PRIVATE
    lifecycle: CommunicationLifecycle = CommunicationLifecycle.ACTIVE
    text_hash: str | None = None
    text_present: bool = False
    text_length: int = 0
    provenance: str = "gateway"
    provenance_version: int = 1
    retracted_by_event_id: str | None = None

    def __post_init__(self) -> None:
        _require_opaque_id(self.event_id, name="event_id")
        _require_opaque_id(self.source_id, name="source_id")
        _require_short_text(self.platform, name="platform", maximum=32)
        _require_short_text(self.provenance, name="provenance", maximum=64)
        _require_machine_token(self.platform, name="platform")
        _require_machine_token(self.provenance, name="provenance")
        _reject_raw_artifact_text(self.platform, name="platform")
        _reject_raw_artifact_text(self.provenance, name="provenance")
        if self.text_hash is not None:
            _require_opaque_id(self.text_hash, name="text_hash")
        if self.retracted_by_event_id is not None:
            _require_opaque_id(self.retracted_by_event_id, name="retracted_by_event_id")
        if isinstance(self.occurred_at, bool) or not isinstance(self.occurred_at, (int, float)):
            raise ValueError("occurred_at must be numeric")
        normalized_occurred_at = float(self.occurred_at)
        if not math.isfinite(normalized_occurred_at):
            raise ValueError("occurred_at must be finite")
        if isinstance(self.occurred_at, int) and normalized_occurred_at != self.occurred_at:
            raise ValueError("occurred_at integer must be exactly representable as a float")
        object.__setattr__(self, "occurred_at", normalized_occurred_at)
        if not isinstance(self.direction, CommunicationDirection):
            raise ValueError("direction is invalid")
        if not isinstance(self.kind, CommunicationKind):
            raise ValueError("kind is invalid")
        if not isinstance(self.actor_role, CommunicationActorRole):
            raise ValueError("actor_role is invalid")
        is_reaction = self.kind in {
            CommunicationKind.REACTION_ADD, CommunicationKind.REACTION_REMOVE
        }
        if is_reaction != isinstance(self.reaction_subtype, CommunicationReactionSubtype):
            raise ValueError("reaction_subtype is required only for reaction events")
        if self.reaction_subtype is CommunicationReactionSubtype.LEGACY_UNTYPED:
            raise ValueError("legacy_untyped is reserved for migrated historical reactions")
        if not isinstance(self.privacy, CommunicationPrivacy):
            raise ValueError("privacy is invalid")
        if not isinstance(self.lifecycle, CommunicationLifecycle):
            raise ValueError("lifecycle is invalid")
        if not isinstance(self.text_present, bool):
            raise ValueError("text_present must be boolean")
        if isinstance(self.text_length, bool) or not isinstance(self.text_length, int):
            raise ValueError("text_length must be an integer")
        if self.text_length < 0 or self.text_length > 1_000_000:
            raise ValueError("text_length is outside the allowed range")
        if self.text_present != (self.text_hash is not None):
            raise ValueError("text_present and text_hash must agree")
        if self.text_present and self.text_length == 0:
            raise ValueError("text_length must be positive when text is present")
        if not self.text_present and self.text_length != 0:
            raise ValueError("text_length requires text evidence")
        if isinstance(self.provenance_version, bool) or not isinstance(self.provenance_version, int):
            raise ValueError("provenance_version must be an integer")
        if self.provenance_version < 1:
            raise ValueError("provenance_version must be positive")
        if self.lifecycle is CommunicationLifecycle.ACTIVE and self.retracted_by_event_id is not None:
            raise ValueError("active events cannot name a retraction event")
        if self.lifecycle is CommunicationLifecycle.RETRACTED and self.retracted_by_event_id is None:
            raise ValueError("retracted events require retracted_by_event_id")

    @classmethod
    def _from_migrated_legacy(cls, **values: Any) -> CommunicationEvent:
        """Restore a validated historical row whose v5 source had no reaction subtype."""
        if values.get("reaction_subtype") is not CommunicationReactionSubtype.LEGACY_UNTYPED:
            raise ValueError("historical restoration requires legacy_untyped")
        validated = cls(
            **{**values, "reaction_subtype": CommunicationReactionSubtype.LIKE}
        )
        object.__setattr__(
            validated, "reaction_subtype", CommunicationReactionSubtype.LEGACY_UNTYPED
        )
        return validated


@dataclass(frozen=True)
class CommunicationUrl:
    url_id: str
    event_id: str
    url_identity: str
    domain: str
    sharer_role: CommunicationActorRole
    enrichment_state: CommunicationEnrichmentState = CommunicationEnrichmentState.PENDING
    platform: str | None = None

    def __post_init__(self) -> None:
        for name in ("url_id", "event_id", "url_identity"):
            _require_opaque_id(getattr(self, name), name=name)
        _require_short_text(self.domain, name="domain", maximum=253)
        if (
            "://" in self.domain or any(character in self.domain for character in "/?#@")
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", self.domain)
        ):
            raise ValueError("domain must be a lowercase hostname without URL material")
        if self.platform is not None:
            _require_short_text(self.platform, name="platform", maximum=32)
            _require_machine_token(self.platform, name="platform")
            _reject_raw_artifact_text(self.platform, name="platform")
        if not isinstance(self.sharer_role, CommunicationActorRole):
            raise ValueError("sharer_role is invalid")
        if not isinstance(self.enrichment_state, CommunicationEnrichmentState):
            raise ValueError("enrichment_state is invalid")


@dataclass(frozen=True)
class CommunicationAttachment:
    attachment_id: str
    event_id: str
    attachment_identity: str
    media_kind: str
    mime_type: str | None = None
    uti: str | None = None
    size_bytes: int | None = None
    caption_present: bool = False
    caption_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("attachment_id", "event_id", "attachment_identity"):
            _require_opaque_id(getattr(self, name), name=name)
        if self.media_kind not in {"image", "video", "audio", "document", "other"}:
            raise ValueError("media_kind is invalid")
        if self.mime_type is not None:
            _require_short_text(self.mime_type, name="mime_type", maximum=127)
            if not re.fullmatch(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*", self.mime_type):
                raise ValueError("mime_type must be a normalized media type")
        if self.uti is not None:
            _require_short_text(self.uti, name="uti", maximum=127)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", self.uti):
                raise ValueError("uti must be a normalized type identifier")
        if self.size_bytes is not None:
            if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
                raise ValueError("size_bytes must be an integer")
            if not 0 <= self.size_bytes <= 10_000_000_000:
                raise ValueError("size_bytes is outside the allowed range")
        if self.caption_hash is not None:
            _require_opaque_id(self.caption_hash, name="caption_hash")
        if self.caption_present != (self.caption_hash is not None):
            raise ValueError("caption_present and caption_hash must agree")


@dataclass(frozen=True)
class CommunicationRelation:
    relation_id: str
    event_id: str
    relation_type: CommunicationRelationType
    target_source_id: str
    target_actor_role: CommunicationActorRole | None = None

    def __post_init__(self) -> None:
        for name in ("relation_id", "event_id", "target_source_id"):
            _require_opaque_id(getattr(self, name), name=name)
        if not isinstance(self.relation_type, CommunicationRelationType):
            raise ValueError("relation_type is invalid")
        if self.target_actor_role is not None and not isinstance(
            self.target_actor_role, CommunicationActorRole
        ):
            raise ValueError("target_actor_role is invalid")


@dataclass(frozen=True)
class EntityMention:
    mention_id: str
    event_id: str
    entity_identity: str
    entity_type: str
    canonical_label: str
    confidence: float
    source_method: str
    surface_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("mention_id", "event_id", "entity_identity"):
            _require_opaque_id(getattr(self, name), name=name)
        if self.entity_type not in {"person", "place", "organization", "thing", "event"}:
            raise ValueError("entity_type is invalid")
        _require_short_text(self.canonical_label, name="canonical_label", maximum=120)
        _require_short_text(self.source_method, name="source_method", maximum=32)
        _require_machine_token(self.source_method, name="source_method")
        _reject_raw_artifact_text(self.canonical_label, name="canonical_label")
        _reject_credential_like_entity_label(self.canonical_label)
        _reject_raw_artifact_text(self.source_method, name="source_method")
        if self.surface_hash is not None:
            _require_opaque_id(self.surface_hash, name="surface_hash")
        if not math.isfinite(float(self.confidence)) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")


@dataclass(frozen=True)
class CommunicationRecommendationEvent:
    recommendation_event_id: str
    recommendation_id: str
    event_id: str
    outcome: CommunicationRecommendationOutcome
    confidence: float
    explicit_linkage: bool

    def __post_init__(self) -> None:
        for name in ("recommendation_event_id", "event_id"):
            _require_opaque_id(getattr(self, name), name=name)
        _require_recommendation_id(self.recommendation_id)
        if not isinstance(self.outcome, CommunicationRecommendationOutcome):
            raise ValueError("outcome is invalid")
        if not math.isfinite(float(self.confidence)) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if not isinstance(self.explicit_linkage, bool):
            raise ValueError("explicit_linkage must be boolean")


@dataclass(frozen=True)
class CommunicationBundle:
    event: CommunicationEvent
    urls: tuple[CommunicationUrl, ...] = ()
    attachments: tuple[CommunicationAttachment, ...] = ()
    relations: tuple[CommunicationRelation, ...] = ()
    entity_mentions: tuple[EntityMention, ...] = ()
    recommendation_events: tuple[CommunicationRecommendationEvent, ...] = ()


@dataclass(frozen=True)
class CommunicationIngestResult:
    event: CommunicationEvent
    inserted: bool
    deduplicated: bool


@dataclass(frozen=True)
class InterestProjection:
    topic: str
    signal_type: SignalType
    valence: InterestValence
    confidence: float
    source_method: ProjectionMethod


@dataclass(frozen=True)
class EntityProjection:
    canonical_label: str
    entity_type: str
    confidence: float
    source_method: ProjectionMethod


@dataclass(frozen=True)
class RecommendationProjection:
    semantic_key: str
    outcome: CommunicationRecommendationOutcome
    confidence: float
    source_method: ProjectionMethod
    explicit_linkage: bool
    topic: str | None = None
    recommendation: str | None = None


@dataclass(frozen=True)
class CallbackProjection:
    semantic_key: str
    canonical_label: str
    confidence: float
    source_method: ProjectionMethod
    supporting_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommunicationProjection:
    interests: tuple[InterestProjection, ...] = ()
    entities: tuple[EntityProjection, ...] = ()
    recommendations: tuple[RecommendationProjection, ...] = ()
    callbacks: tuple[CallbackProjection, ...] = ()


@dataclass(frozen=True)
class CommunicationProjectionResult:
    event_id: str
    projector_version: str
    identities: tuple[str, ...]
    inserted: bool
    deduplicated: bool
    retracted: bool = False


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
  folded_at REAL,
  origin_communication_event_id TEXT REFERENCES communication_event(event_id),
  projector_version TEXT,
  projection_kind TEXT,
  semantic_key TEXT,
  confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  source_method TEXT CHECK(source_method IS NULL OR source_method IN ('deterministic','model')),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  original_topic_text TEXT
);
CREATE INDEX IF NOT EXISTS interest_event_unfolded ON interest_event(folded_at, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS communication_interest_projection
  ON interest_event(origin_communication_event_id,projector_version,projection_kind,semantic_key)
  WHERE origin_communication_event_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS import_run (
  run_id TEXT PRIMARY KEY,
  source_hash TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  fact_count INTEGER NOT NULL,
  interest_count INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS import_run_source ON import_run(source_hash);
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
CREATE TABLE IF NOT EXISTS interest_projection_baseline (
  topic TEXT PRIMARY KEY,
  baseline_exists INTEGER NOT NULL CHECK(baseline_exists IN (0,1)),
  interest_id TEXT,
  raw_score REAL,
  last_evidence_at REAL,
  evidence_count INTEGER,
  valence TEXT CHECK(valence IS NULL OR valence IN ('positive','negative','neutral')),
  ts_alpha REAL,
  ts_beta REAL,
  updated_at REAL,
  CHECK((baseline_exists=0 AND interest_id IS NULL AND raw_score IS NULL
         AND last_evidence_at IS NULL AND evidence_count IS NULL AND valence IS NULL
         AND ts_alpha IS NULL AND ts_beta IS NULL AND updated_at IS NULL) OR
        (baseline_exists=1 AND interest_id IS NOT NULL AND raw_score IS NOT NULL
         AND last_evidence_at IS NOT NULL AND evidence_count IS NOT NULL AND valence IS NOT NULL
         AND ts_alpha IS NOT NULL AND ts_beta IS NOT NULL AND updated_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS proactive_send (
  send_id TEXT PRIMARY KEY,
  interest_id TEXT REFERENCES interest(interest_id),
  kind TEXT NOT NULL CHECK(kind IN ('interest_share','checkin','exploration')),
  candidate_json TEXT NOT NULL,
  item_hash TEXT,
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
CREATE INDEX IF NOT EXISTS proactive_send_item_hash
  ON proactive_send(item_hash) WHERE item_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS communication_event (
  event_id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  source_id TEXT NOT NULL,
  occurred_at REAL NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
  kind TEXT NOT NULL CHECK(kind IN (
    'text','link_share','attachment_share','reaction_add','reaction_remove','reply',
    'batch_member','recommendation','follow_through','callback')),
  reaction_subtype TEXT CHECK(reaction_subtype IN (
    'like','love','dislike','laugh','emphasis','question','legacy_untyped')),
  actor_role TEXT NOT NULL CHECK(actor_role IN ('contact','counterpart','assistant')),
  privacy TEXT NOT NULL CHECK(privacy IN ('private','sensitive','restricted')),
  lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','retracted')),
  text_hash TEXT,
  text_present INTEGER NOT NULL CHECK(text_present IN (0,1)),
  text_length INTEGER NOT NULL CHECK(
    typeof(text_length)='integer' AND text_length >= 0 AND text_length <= 1000000),
  provenance TEXT NOT NULL,
  provenance_version INTEGER NOT NULL CHECK(
    typeof(provenance_version)='integer' AND provenance_version >= 1),
  retracted_by_event_id TEXT REFERENCES communication_event(event_id),
  UNIQUE(platform, source_id),
  CHECK((text_present=1 AND text_hash IS NOT NULL AND text_length > 0) OR
        (text_present=0 AND text_hash IS NULL AND text_length=0)),
  CHECK((lifecycle='active' AND retracted_by_event_id IS NULL) OR
        (lifecycle='retracted' AND retracted_by_event_id IS NOT NULL)),
  CHECK((kind IN ('reaction_add','reaction_remove') AND reaction_subtype IS NOT NULL) OR
        (kind NOT IN ('reaction_add','reaction_remove') AND reaction_subtype IS NULL))
);
CREATE INDEX IF NOT EXISTS communication_event_occurred
  ON communication_event(occurred_at, event_id);
CREATE INDEX IF NOT EXISTS communication_event_lifecycle
  ON communication_event(lifecycle, occurred_at);
CREATE TRIGGER IF NOT EXISTS communication_event_reaction_subtype_insert
BEFORE INSERT ON communication_event
WHEN (NEW.kind IN ('reaction_add','reaction_remove')) != (NEW.reaction_subtype IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'reaction subtype must match reaction kind');
END;
CREATE TRIGGER IF NOT EXISTS communication_event_reaction_subtype_update
BEFORE UPDATE OF kind,reaction_subtype ON communication_event
WHEN (NEW.kind IN ('reaction_add','reaction_remove')) != (NEW.reaction_subtype IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'reaction subtype must match reaction kind');
END;
CREATE TABLE IF NOT EXISTS communication_url (
  url_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES communication_event(event_id) ON DELETE CASCADE,
  url_identity TEXT NOT NULL,
  domain TEXT NOT NULL,
  sharer_role TEXT NOT NULL CHECK(sharer_role IN ('contact','counterpart','assistant')),
  enrichment_state TEXT NOT NULL CHECK(enrichment_state IN ('pending','reviewed','rejected','unavailable')),
  platform TEXT,
  UNIQUE(event_id, url_identity)
);
CREATE TABLE IF NOT EXISTS communication_attachment (
  attachment_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES communication_event(event_id) ON DELETE CASCADE,
  attachment_identity TEXT NOT NULL,
  media_kind TEXT NOT NULL CHECK(media_kind IN ('image','video','audio','document','other')),
  mime_type TEXT,
  uti TEXT,
  size_bytes INTEGER CHECK(size_bytes IS NULL OR (
    typeof(size_bytes)='integer' AND size_bytes >= 0 AND size_bytes <= 10000000000)),
  caption_present INTEGER NOT NULL CHECK(caption_present IN (0,1)),
  caption_hash TEXT,
  UNIQUE(event_id, attachment_identity),
  CHECK((caption_present=1 AND caption_hash IS NOT NULL) OR
        (caption_present=0 AND caption_hash IS NULL))
);
CREATE TABLE IF NOT EXISTS communication_relation (
  relation_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES communication_event(event_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL CHECK(relation_type IN (
    'reply_to','reaction_to','batch_member_of','recommends','follows_through','callback_to')),
  target_source_id TEXT NOT NULL,
  target_actor_role TEXT CHECK(target_actor_role IN ('contact','counterpart','assistant')),
  UNIQUE(event_id, relation_type, target_source_id)
);
CREATE INDEX IF NOT EXISTS communication_relation_target
  ON communication_relation(target_source_id, relation_type);
CREATE TABLE IF NOT EXISTS communication_retraction_pending (
  retraction_event_id TEXT PRIMARY KEY
    REFERENCES communication_event(event_id) ON DELETE CASCADE,
  target_event_id TEXT NOT NULL UNIQUE CHECK(
    length(target_event_id)=64 AND target_event_id NOT GLOB '*[^0-9a-f]*')
);
CREATE TABLE IF NOT EXISTS entity_mention (
  mention_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES communication_event(event_id) ON DELETE CASCADE,
  entity_identity TEXT NOT NULL,
  entity_type TEXT NOT NULL CHECK(entity_type IN ('person','place','organization','thing','event')),
  canonical_label TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  source_method TEXT NOT NULL,
  surface_hash TEXT,
  UNIQUE(event_id, entity_identity, source_method)
);
CREATE TABLE IF NOT EXISTS communication_recommendation_event (
  recommendation_event_id TEXT PRIMARY KEY,
  recommendation_id TEXT NOT NULL,
  event_id TEXT NOT NULL REFERENCES communication_event(event_id) ON DELETE CASCADE,
  outcome TEXT NOT NULL CHECK(outcome IN ('proposed','accepted','rejected','revisited','fulfilled')),
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  explicit_linkage INTEGER NOT NULL CHECK(explicit_linkage IN (0,1)),
  projector_version TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  UNIQUE(recommendation_id, event_id, outcome)
);
CREATE TABLE IF NOT EXISTS projected_entity (
  projection_id TEXT PRIMARY KEY,
  origin_communication_event_id TEXT NOT NULL REFERENCES communication_event(event_id),
  projector_version TEXT NOT NULL,
  normalized_key TEXT NOT NULL,
  entity_type TEXT NOT NULL CHECK(entity_type IN ('person','place','organization','thing','event')),
  canonical_label TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  source_method TEXT NOT NULL CHECK(source_method IN ('deterministic','model')),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  created_at REAL NOT NULL,
  UNIQUE(origin_communication_event_id,projector_version,normalized_key)
);
CREATE INDEX IF NOT EXISTS projected_entity_key
  ON projected_entity(normalized_key,active,created_at);
CREATE TABLE IF NOT EXISTS projected_recommendation (
  recommendation_id TEXT PRIMARY KEY REFERENCES recommendation(recommendation_id),
  origin_communication_event_id TEXT NOT NULL REFERENCES communication_event(event_id),
  projector_version TEXT NOT NULL,
  semantic_key TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  UNIQUE(origin_communication_event_id,projector_version,semantic_key)
);
CREATE TABLE IF NOT EXISTS semantic_callback (
  callback_id TEXT PRIMARY KEY,
  origin_communication_event_id TEXT NOT NULL REFERENCES communication_event(event_id),
  projector_version TEXT NOT NULL,
  semantic_key TEXT NOT NULL,
  canonical_label TEXT NOT NULL,
  privacy TEXT NOT NULL CHECK(privacy='restricted'),
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  source_method TEXT NOT NULL CHECK(source_method IN ('deterministic','model')),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  created_at REAL NOT NULL,
  UNIQUE(origin_communication_event_id,projector_version,semantic_key)
);
CREATE INDEX IF NOT EXISTS semantic_callback_active
  ON semantic_callback(active,created_at);
CREATE TABLE IF NOT EXISTS semantic_callback_support (
  callback_id TEXT NOT NULL REFERENCES semantic_callback(callback_id) ON DELETE CASCADE,
  communication_event_id TEXT NOT NULL REFERENCES communication_event(event_id),
  PRIMARY KEY(callback_id,communication_event_id)
);
CREATE TABLE IF NOT EXISTS communication_projection_receipt (
  communication_event_id TEXT NOT NULL REFERENCES communication_event(event_id),
  projector_version TEXT NOT NULL,
  proposal_hash TEXT NOT NULL,
  projection_count INTEGER NOT NULL,
  replay_sequence INTEGER NOT NULL,
  projected_at REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  PRIMARY KEY(communication_event_id,projector_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_communication_projection
  ON communication_projection_receipt(communication_event_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS interest_topic_alias (
  alias_topic TEXT PRIMARY KEY,
  canonical_topic TEXT NOT NULL
);
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
