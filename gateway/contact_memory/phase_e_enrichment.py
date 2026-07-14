"""Deterministic, privacy-bounded Phase E v3 historical enrichment.

Raw text is accepted only by ``derive_scan_enrichment`` and is never retained in
returned objects. Outputs contain closed ontology IDs, typed public entities,
opaque support IDs, bounded gate codes, and aggregate counts.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import hmac
import re
from typing import Any, Mapping, Sequence

from .imessage_communication_adapter import HistoricalCommunicationScan
from .phase_e_taxonomy import (
    ONTOLOGY_VERSION,
    PLATFORM_ENTITY_DENYLIST,
    PUBLIC_ENTITIES,
    RULE_VERSION,
    TAXONOMY,
    TAXONOMY_COMMITMENT,
)
from .schema import (
    CommunicationActorRole,
    CommunicationKind,
    CommunicationReactionSubtype,
    CommunicationRelationType,
)

DERIVATION_VERSION = "phase-e-enrichment-v3.0.0"
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|im|i’ve|i've|my|me|mine|we|we're|our|ours)\b", re.I)
_THIRD_PARTY = re.compile(r"\b(?:he|she|his|her|hers|they|their|theirs|steve|stephen|kosta|my friend|my mom|my sister)\b", re.I)
_QUESTION = re.compile(r"(?:\?|\b(?:who|what|when|where|why|how|do you|did you|would you|could you|should we|are you|is it)\b)", re.I)
_NEGATIVE = re.compile(r"\b(?:hate|hated|dislike|disliked|don't like|dont like|do not like|not into|can't stand|cannot stand|avoid|avoiding|never want)\b", re.I)
_POSITIVE = re.compile(r"\b(?:love|loved|like|liked|enjoy|enjoyed|favorite|favourite|obsessed|excited|can't wait|cannot wait|really want|so good|amazing|awesome)\b", re.I)
_ROUTINE = re.compile(r"\b(?:i|we|my|our)\b.{0,40}\b(?:go|going|went|do|doing|did|train|training|workout|watch|watching|eat|eating|drive|driving|walk|walking|play|playing)\b", re.I)
_LOGISTICS = re.compile(r"\b(?:pick up|drop off|appointment|reservation|booked|booking|need to get|have to get|service center|repair|mechanic|airport pickup|flight time|boarding|check in|parking|traffic|directions|ticket transfer)\b", re.I)
_SARCASM = re.compile(r"\b(?:yeah right|sure jan|as if|sarcasm|jk|just kidding|lol|lmao|rofl)\b", re.I)
_PUBLIC_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .&+'-]{0,78}[A-Za-z0-9]$")


@dataclass(frozen=True)
class DerivedSupport:
    subject: str
    event_id: str
    source_id: str
    support_id: str
    recurrence_support_id: str
    semantic_kind: str
    semantic_key: str
    canonical_label: str
    entity_type: str | None
    actor_code: str
    polarity: str
    stance_code: str
    gate_codes: tuple[str, ...]
    rule_code: str
    occurred_day: int
    authenticated_target: bool


@dataclass(frozen=True)
class EnrichmentCandidate:
    subject: str
    kind: str
    semantic_key: str
    label: str
    entity_type: str | None
    polarity: str
    eligibility: str
    supports: tuple[DerivedSupport, ...]
    distinct_days: int
    positive_supports: int


@dataclass(frozen=True)
class EnrichmentResult:
    candidates: tuple[EnrichmentCandidate, ...]
    watchlist: tuple[Mapping[str, object], ...]
    exclusions: Mapping[str, int]
    metrics: Mapping[str, object]
    versions: Mapping[str, str]


def _opaque(secret: bytes, namespace: str, *parts: str) -> str:
    if len(secret) < 16:
        raise ValueError("HMAC secret must be at least 16 bytes")
    payload = namespace + "\0" + "\0".join(parts)
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalize_text(raw: object) -> str:
    # This value is ephemeral and must never be returned or interpolated into errors.
    text = _URL.sub(" ", str(raw or "")).casefold().replace("’", "'")
    return _SPACE.sub(" ", text).strip()


def _phrase_matches(text: str) -> list[tuple[str, str, str, str]]:
    matches: list[tuple[int, int, int, str, str, str, str]] = []
    for rule in TAXONOMY:
        if any(re.search(rf"(?<!\w){re.escape(item)}(?!\w)", text) for item in rule.exclusions):
            continue
        for phrase in rule.phrases:
            found = re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text)
            if found:
                matches.append((
                    -(found.end() - found.start()), found.start(), found.end(),
                    rule.ontology_id, rule.label, rule.lane, phrase,
                ))
    # Longest phrase wins within one ontology ID. Different ontology lanes may
    # intentionally share the same evidence (for example training + activity).
    selected: dict[str, tuple[int, int, int, str, str, str, str]] = {}
    for item in sorted(matches):
        selected.setdefault(item[3], item)
    return [(item[3], item[4], item[5], item[6]) for item in selected.values()]


def _entity_matches(text: str) -> list[tuple[str, str, str]]:
    matches: list[tuple[int, str, str, str]] = []
    for entity in PUBLIC_ENTITIES:
        if entity.canonical_label.casefold() in PLATFORM_ENTITY_DENYLIST:
            continue
        if not _PUBLIC_SHAPE.fullmatch(entity.canonical_label):
            continue
        for alias in entity.aliases:
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text):
                matches.append((-len(alias), entity.canonical_label, entity.entity_type, alias))
                break
    seen: set[tuple[str, str]] = set()
    result = []
    for _size, label, entity_type, alias in sorted(matches):
        key = (label.casefold(), entity_type)
        if key not in seen:
            seen.add(key)
            result.append((label, entity_type, alias))
    return result


def _stance(text: str) -> tuple[str, str, tuple[str, ...]]:
    gates: list[str] = []
    first = bool(_FIRST_PERSON.search(text))
    question = bool(_QUESTION.search(text))
    third_party = bool(_THIRD_PARTY.search(text)) and not first
    logistics = bool(_LOGISTICS.search(text))
    sarcasm = bool(_SARCASM.search(text))
    if question:
        gates.append("question")
    if third_party:
        gates.append("third_party")
    if logistics:
        gates.append("logistics")
    if sarcasm:
        gates.append("sarcasm_risk")
    if _NEGATIVE.search(text) and first and not question:
        return "negative", "first_person_negative", tuple(gates)
    if _POSITIVE.search(text) and first and not question and not third_party:
        if sarcasm:
            return "ambiguous", "sarcasm_review", tuple(gates)
        return "positive", "first_person_positive", tuple(gates)
    if _ROUTINE.search(text) and first and not question and not third_party:
        return "neutral", "first_person_routine", tuple(gates)
    return "neutral", "mention_only", tuple(gates)


def _batch_unit(record: Any) -> str:
    for relation in record.bundle.relations:
        if relation.relation_type is CommunicationRelationType.BATCH_MEMBER_OF:
            return relation.target_source_id
    return record.bundle.event.event_id


def _support(
    *,
    record: Any,
    secret: bytes,
    semantic_kind: str,
    semantic_key: str,
    label: str,
    entity_type: str | None,
    polarity: str,
    stance_code: str,
    gates: Sequence[str],
    rule_code: str,
    authenticated_target: bool = False,
) -> DerivedSupport:
    event = record.bundle.event
    recurrence_unit = _batch_unit(record)
    actor_code = "authenticated_author"
    support_id = _opaque(
        secret, "phase-e-support-v3", record.author, event.event_id, semantic_kind,
        semantic_key, polarity, rule_code, ONTOLOGY_VERSION, RULE_VERSION,
        DERIVATION_VERSION,
    )
    recurrence_id = _opaque(
        secret, "phase-e-recurrence-v3", record.author, recurrence_unit,
        semantic_kind, semantic_key, DERIVATION_VERSION,
    )
    return DerivedSupport(
        subject=record.author, event_id=event.event_id, source_id=event.source_id,
        support_id=support_id, recurrence_support_id=recurrence_id,
        semantic_kind=semantic_kind, semantic_key=semantic_key,
        canonical_label=label, entity_type=entity_type, actor_code=actor_code,
        polarity=polarity, stance_code=stance_code,
        gate_codes=tuple(sorted(set(gates))), rule_code=rule_code,
        occurred_day=int(event.occurred_at // 86_400),
        authenticated_target=authenticated_target,
    )


def derive_scan_enrichment(
    scan: HistoricalCommunicationScan,
    *,
    secret: bytes,
    existing_topics: Mapping[str, frozenset[str]] | None = None,
    existing_entities: Mapping[str, frozenset[str]] | None = None,
) -> EnrichmentResult:
    """Derive closed-world candidates; no raw source value survives this call."""
    if len(secret) < 16:
        raise ValueError("HMAC secret must be at least 16 bytes")
    existing_topics = existing_topics or {}
    existing_entities = existing_entities or {}
    exclusions: Counter[str] = Counter()
    supports: list[DerivedSupport] = []
    semantic_by_source: dict[str, tuple[DerivedSupport, ...]] = {}
    record_by_source = {record.bundle.event.source_id: record for record in scan.records}
    v3_matched_events: set[str] = set()
    v3_matched_text_events: set[str] = set()
    v3_matched_plain_text_events: set[str] = set()
    text_eligible = 0
    plain_text_eligible = 0

    # Pass 1: authenticated authored visible text only. Raw text is scoped to one
    # loop iteration and is neither logged nor retained in a returned object.
    for record in scan.records:
        event = record.bundle.event
        if event.kind not in {
            CommunicationKind.TEXT, CommunicationKind.REPLY,
            CommunicationKind.LINK_SHARE, CommunicationKind.ATTACHMENT_SHARE,
        }:
            continue
        raw = record.private_evidence.get("text")
        text = _normalize_text(raw)
        raw = None
        if not text:
            continue
        if event.kind is CommunicationKind.TEXT:
            text_eligible += 1
            if record.private_evidence.get("text_source") == "plain":
                plain_text_eligible += 1
        polarity, stance_code, gates = _stance(text)
        if stance_code == "first_person_routine" and any(
            relation.relation_type is CommunicationRelationType.BATCH_MEMBER_OF
            for relation in record.bundle.relations
        ):
            gates = tuple((*gates, "batch_continuation"))
        matched: list[DerivedSupport] = []
        phrase_matches = _phrase_matches(text)

        for ontology_id, label, lane, phrase in phrase_matches:
            support = _support(
                record=record, secret=secret,
                semantic_kind="activity" if lane == "activity" else "topic",
                semantic_key=ontology_id, label=label, entity_type=None,
                polarity=polarity, stance_code=stance_code, gates=gates,
                rule_code=f"phrase:{ontology_id}:{phrase}",
            )
            matched.append(support)
        for label, entity_type, alias in _entity_matches(text):
            normalized = label.casefold()
            if normalized in PLATFORM_ENTITY_DENYLIST:
                exclusions["platform_entity_denied"] += 1
                continue
            matched.append(_support(
                record=record, secret=secret, semantic_kind="entity",
                semantic_key=f"{entity_type}:{normalized}", label=label,
                entity_type=entity_type, polarity=polarity,
                stance_code=stance_code, gates=gates,
                rule_code=f"public_entity:{entity_type}:{alias}",
            ))
        text = ""
        if matched:
            v3_matched_events.add(event.event_id)
            if event.kind is CommunicationKind.TEXT:
                v3_matched_text_events.add(event.event_id)
                if record.private_evidence.get("text_source") == "plain":
                    v3_matched_plain_text_events.add(event.event_id)
            semantic_by_source[event.source_id] = tuple(matched)
            # Replies and shares do not independently establish a durable
            # preference. Pass 2 may admit their closed semantics only through
            # an authenticated counterpart target relation.
            if event.kind is CommunicationKind.TEXT:
                supports.extend(matched)

    # Pass 2: replies/reactions borrow target semantics only through an exact,
    # authenticated canonical relation. Plain replies and laugh/emphasis taps do not.
    for record in scan.records:
        event = record.bundle.event
        relation = next((
            item for item in record.bundle.relations
            if item.relation_type in {
                CommunicationRelationType.REPLY_TO,
                CommunicationRelationType.REACTION_TO,
            }
            and item.target_actor_role is CommunicationActorRole.COUNTERPART
        ), None)
        if relation is None or relation.target_source_id not in record_by_source:
            continue
        target_supports = semantic_by_source.get(relation.target_source_id, ())
        if not target_supports:
            continue
        authenticated = True
        accepted = False
        stance_code = "neutral_relation"
        polarity = "neutral"
        if event.kind is CommunicationKind.REACTION_ADD and event.reaction_subtype in {
            CommunicationReactionSubtype.LOVE, CommunicationReactionSubtype.LIKE,
        }:
            accepted, stance_code, polarity = True, "positive_reaction", "positive"
        elif event.kind is CommunicationKind.REPLY:
            text = _normalize_text(record.private_evidence.get("text"))
            reply_polarity, reply_stance, reply_gates = _stance(text)
            text = ""
            accepted = reply_polarity == "positive" and not reply_gates
            stance_code, polarity = reply_stance, reply_polarity
        if not accepted:
            exclusions["neutral_or_unauthenticated_relation"] += 1
            continue
        for target in target_supports:
            if target.semantic_kind not in {"topic", "activity", "entity"}:
                continue
            supports.append(_support(
                record=record, secret=secret, semantic_kind=target.semantic_kind,
                semantic_key=target.semantic_key, label=target.canonical_label,
                entity_type=target.entity_type, polarity=polarity,
                stance_code=stance_code, gates=(),
                rule_code=f"authenticated_target:{target.rule_code}",
                authenticated_target=authenticated,
            ))
            v3_matched_events.add(event.event_id)

    grouped: dict[tuple[str, str, str], list[DerivedSupport]] = defaultdict(list)
    for support in supports:
        grouped[(support.subject, support.semantic_kind, support.semantic_key)].append(support)

    candidates: list[EnrichmentCandidate] = []
    watchlist: list[Mapping[str, object]] = []
    for (subject, kind, key), raw_group in sorted(grouped.items()):
        # One semantic support per event, and one recurrence vote per rapid batch.
        event_unique = {item.event_id: item for item in raw_group}
        group = tuple(sorted(event_unique.values(), key=lambda item: item.event_id))
        recurrence = {item.recurrence_support_id for item in group}
        days = {item.occurred_day for item in group}
        label = group[0].canonical_label
        entity_type = group[0].entity_type
        positive = [item for item in group if item.polarity == "positive"]
        negative = [item for item in group if item.polarity == "negative"]
        routine = [item for item in group if item.stance_code == "first_person_routine"]
        gated = [item for item in group if item.gate_codes]
        if kind in {"topic", "activity"} and label.casefold() in existing_topics.get(subject, frozenset()):
            exclusions["existing_reviewed_topic"] += 1
            continue
        if kind == "entity" and label.casefold() in existing_entities.get(subject, frozenset()):
            exclusions["existing_reviewed_entity"] += 1
            continue
        polarity = "mixed" if positive and negative else (
            "negative" if negative and not positive else "positive" if positive else "neutral"
        )
        qualified: tuple[DerivedSupport, ...] = group
        eligible = len(recurrence) >= 2 and len(days) >= 2
        if kind == "topic":
            qualified = tuple(
                item for item in group
                if not item.gate_codes and item.polarity in {"positive", "negative"}
            )
            qualified_days = {item.occurred_day for item in qualified}
            qualified_recurrence = {item.recurrence_support_id for item in qualified}
            eligible = (
                len(qualified_recurrence) >= 2 and len(qualified_days) >= 2
                and polarity != "mixed"
            )
        elif kind == "activity":
            qualified = tuple(
                item for item in group
                if not item.gate_codes and (
                    item.polarity == "positive"
                    or item.stance_code == "first_person_routine"
                )
            )
            eligible = (
                len({item.recurrence_support_id for item in qualified}) >= 2
                and len({item.occurred_day for item in qualified}) >= 2
                and polarity != "mixed"
            )
        elif kind == "entity":
            eligible = eligible and entity_type is not None and label.casefold() not in PLATFORM_ENTITY_DENYLIST
        if eligible:
            candidates.append(EnrichmentCandidate(
                subject=subject, kind=kind, semantic_key=key, label=label,
                entity_type=entity_type, polarity=polarity,
                eligibility="eligible", supports=qualified,
                distinct_days=len({item.occurred_day for item in qualified}),
                positive_supports=len(positive),
            ))
        else:
            reason = (
                "gated_context" if gated else "mixed_polarity" if polarity == "mixed"
                else "no_positive_preference" if kind == "topic" and not (positive or negative)
                else "insufficient_recurrence"
            )
            exclusions[f"watchlist_{reason}"] += 1
            watchlist.append({
                "subject": subject, "kind": kind, "semantic_key": key,
                "label": label, "entity_type": entity_type,
                "occurrence_count": len(group), "distinct_days": len(days),
                "positive_supports": len(positive), "negative_supports": len(negative),
                "gated_supports": len(gated), "reason": reason,
            })

    by_subject: dict[str, Counter[str]] = defaultdict(Counter)
    for item in candidates:
        by_subject[item.subject][item.kind] += 1
    # Frozen aggregate-only v2 audit baseline; do not recompute it under the
    # v3 decoder or rules because that would silently change the denominator.
    v2_eligible, v2_matched = 1_827, 116
    v3_matched = len(v3_matched_text_events)
    metrics: dict[str, object] = {
        "eligible_standalone_text_events": text_eligible,
        "v2_audit_eligible_standalone_text_events": v2_eligible,
        "v2_matched_events": v2_matched,
        "v3_matched_standalone_text_events": v3_matched,
        "v3_legacy_visible_eligible_events": plain_text_eligible,
        "v3_legacy_visible_matched_events": len(v3_matched_plain_text_events),
        "v3_legacy_visible_coverage_basis_points": round(
            10_000 * len(v3_matched_plain_text_events) / max(plain_text_eligible, 1)
        ),
        "v3_all_semantic_events": len(v3_matched_events),
        "coverage_gain_events": max(0, v3_matched - v2_matched),
        "attributed_root_text_recovery_events": max(0, text_eligible - v2_eligible),
        "v2_coverage_basis_points": v2_matched * 10_000 // v2_eligible,
        "v3_coverage_basis_points": round(10_000 * v3_matched / max(text_eligible, 1)),
        "coverage_gain_basis_points": (
            round(10_000 * len(v3_matched_plain_text_events) / max(plain_text_eligible, 1))
            - (v2_matched * 10_000 // v2_eligible)
        ),
        "eligible_candidates": len(candidates),
        "watchlist_groups": len(watchlist),
        "positive_candidate_share_basis_points": round(
            10_000 * sum(item.polarity == "positive" for item in candidates)
            / max(len(candidates), 1)
        ),
        "platform_entity_candidates": sum(
            item.kind == "entity" and item.label.casefold() in PLATFORM_ENTITY_DENYLIST
            for item in candidates
        ),
        "by_subject": {subject: dict(sorted(counts.items())) for subject, counts in sorted(by_subject.items())},
    }
    return EnrichmentResult(
        candidates=tuple(candidates), watchlist=tuple(watchlist),
        exclusions=dict(sorted(exclusions.items())), metrics=metrics,
        versions={
            "ontology_version": ONTOLOGY_VERSION, "rule_version": RULE_VERSION,
            "derivation_version": DERIVATION_VERSION,
            "taxonomy_commitment": TAXONOMY_COMMITMENT,
        },
    )


__all__ = [
    "DERIVATION_VERSION", "DerivedSupport", "EnrichmentCandidate", "EnrichmentResult",
    "derive_scan_enrichment",
]
