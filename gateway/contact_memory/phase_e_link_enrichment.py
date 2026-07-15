"""Phase E v4 historical link research and closed semantic derivation."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import hmac
import json
from typing import Any, Mapping
import urllib.parse

from .imessage_communication_adapter import (
    HistoricalCommunicationRecord, HistoricalCommunicationScan,
)
from .imessage_link_review import LinkSignal, evidence_id, select_enrichment_queue
from .link_research import LinkResearchProvider, LinkResearchRequest, LinkResearchResult, RESEARCH_VERSION
from .phase_e_enrichment import DerivedSupport, EnrichmentCandidate, EnrichmentResult
from .phase_e_taxonomy import PUBLIC_ENTITIES, TAXONOMY, TAXONOMY_COMMITMENT
from .schema import CommunicationKind, CommunicationReactionSubtype, CommunicationRelationType

LINK_DERIVATION_VERSION = "phase-e-link-enrichment-v4.0.0"
_RULE_BY_ID = {rule.ontology_id: rule for rule in TAXONOMY}
_ENTITY_BY_KEY = {(item.entity_type, item.canonical_label): item for item in PUBLIC_ENTITIES}


@dataclass(frozen=True)
class HistoricalLinkResearch:
    review_cache: Mapping[str, object]
    private_cache: Mapping[str, object]
    aggregate: Mapping[str, object]


def _opaque(secret: bytes, namespace: str, *parts: str) -> str:
    return hmac.new(secret, (namespace + "\0" + "\0".join(parts)).encode(), hashlib.sha256).hexdigest()


def _signals(scan: HistoricalCommunicationScan) -> list[LinkSignal]:
    positive_by_target: dict[str, set[str]] = defaultdict(set)
    replied_by_target: dict[str, set[str]] = defaultdict(set)
    by_source = {record.bundle.event.source_id: record for record in scan.records}
    for record in scan.records:
        event = record.bundle.event
        relation = next((item for item in record.bundle.relations if item.relation_type in {
            CommunicationRelationType.REACTION_TO, CommunicationRelationType.REPLY_TO,
        }), None)
        if relation is None or relation.target_source_id not in by_source:
            continue
        if (
            event.kind is CommunicationKind.REACTION_ADD
            and event.reaction_subtype in {CommunicationReactionSubtype.LIKE, CommunicationReactionSubtype.LOVE}
        ):
            positive_by_target[relation.target_source_id].add(record.author)
        elif event.kind is CommunicationKind.REPLY:
            replied_by_target[relation.target_source_id].add(record.author)
    result: list[LinkSignal] = []
    for record in scan.records:
        event = record.bundle.event
        raw_urls = record.private_evidence.get("urls", ())
        if not isinstance(raw_urls, list):
            continue
        for url in raw_urls:
            if not isinstance(url, str):
                continue
            result.append(LinkSignal(
                identity_url=url,
                fetch_url=url,
                message_guid=event.source_id,
                created_at=event.occurred_at,
                shared_by=record.author,
                domain=(urllib.parse.urlsplit(url).hostname or "").casefold(),
                platform=record.bundle.urls[0].platform if record.bundle.urls else None,
                positive_reactors=tuple(sorted(positive_by_target.get(event.source_id, ()))),
                replied_by=tuple(sorted(replied_by_target.get(event.source_id, ()))),
            ))
    return result


def _result_payload(result: LinkResearchResult) -> dict[str, object]:
    return {
        "status": result.status,
        "ontology_ids": list(result.ontology_ids),
        "public_entities": [list(item) for item in result.public_entities],
        "source_quality": result.source_quality,
        "recency_band": result.recency_band,
        "support_ids": list(result.support_ids),
        "provider_commitment": result.provider_commitment,
        "research_version": RESEARCH_VERSION,
    }


def _research_auth(secret: bytes, evidence: str, payload: Mapping[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "research_auth"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
    return _opaque(secret, "phase-e-link-result-v4", evidence, canonical)


def research_historical_links(
    scan: HistoricalCommunicationScan,
    *,
    secret: bytes,
    provider: LinkResearchProvider,
    max_requests: int = 50,
) -> HistoricalLinkResearch:
    """Research every selected safe link; exact values stay in the private cache."""
    signals = _signals(scan)
    queue = select_enrichment_queue(signals, secret, max_requests=max_requests)
    by_evidence: dict[str, list[LinkSignal]] = defaultdict(list)
    for signal in signals:
        by_evidence[evidence_id(secret, "url", signal.identity_url)].append(signal)
    review: dict[str, Mapping[str, object]] = {}
    private_rows: dict[str, object] = {}
    failures: Counter[str] = Counter()
    recency: Counter[str] = Counter()
    source_classes: Counter[str] = Counter()
    for request in queue["requests"]:
        eid = str(request["evidence_id"])
        occurrences = by_evidence[eid]
        first = occurrences[0]
        result = provider.research(LinkResearchRequest(
            evidence_id=eid,
            url=str(request["url"]),
            platform=str(request["platform"]),
            shared_by=first.shared_by,
            occurred_at=max(item.created_at for item in occurrences),
            positive_reactors=tuple(sorted({actor for item in occurrences for actor in item.positive_reactors})),
            replied_by=tuple(sorted({actor for item in occurrences for actor in item.replied_by})),
            repeated_shares=len(occurrences),
            distinct_days=len({int(item.created_at // 86_400) for item in occurrences}),
        ))
        payload = _result_payload(result)
        payload["research_auth"] = _research_auth(secret, eid, payload)
        review[eid] = payload
        private_rows[eid] = {
            "url": request["url"],
            "metadata": dict(result.metadata),
            "result": payload,
        }
        if result.status != "ok":
            failures[result.status] += 1
        if result.recency_band:
            recency[result.recency_band] += 1
        if result.source_quality:
            source_classes[result.source_quality] += 1
    aggregate = {
        "schema": 1,
        "kind": "phase-e-v4-link-research-aggregate",
        "eligible_safe_links": len(queue["requests"]),
        "successful": sum(item.get("status") == "ok" for item in review.values()),
        "failed": sum(item.get("status") != "ok" for item in review.values()),
        "failures": dict(sorted(failures.items())),
        "recency_bands": dict(sorted(recency.items())),
        "source_classes": dict(sorted(source_classes.items())),
        "raw_urls_retained_in_aggregate": False,
    }
    return HistoricalLinkResearch(
        review_cache={"schema": 1, "kind": "phase-e-v4-link-research-review-cache", "results": review},
        private_cache={"schema": 1, "kind": "private-phase-e-v4-link-research-cache", "entries": private_rows},
        aggregate=aggregate,
    )


def _cache_results(cache: object, *, secret: bytes) -> Mapping[str, Mapping[str, object]]:
    if not isinstance(cache, Mapping):
        return {}
    if cache.get("schema") != 1 or cache.get("kind") != "phase-e-v4-link-research-review-cache":
        return {}
    values = cache.get("results")
    if not isinstance(values, Mapping):
        return {}
    return {
        str(key): value for key, value in values.items()
        if (
            isinstance(key, str) and isinstance(value, Mapping)
            and value.get("research_version") == RESEARCH_VERSION
            and isinstance(value.get("research_auth"), str)
            and hmac.compare_digest(
                str(value["research_auth"]), _research_auth(secret, str(key), value),
            )
            and (
                value.get("status") != "ok"
                or (
                    isinstance(value.get("provider_commitment"), str)
                    and len(str(value["provider_commitment"])) == 64
                    and not set(str(value["provider_commitment"])) - set("0123456789abcdef")
                    and isinstance(value.get("support_ids", ()), list)
                    and all(
                        isinstance(item, str) and len(item) == 64
                        and not set(item) - set("0123456789abcdef")
                        for item in value.get("support_ids", ())
                    )
                )
            )
        )
    }


def derive_link_research_enrichment(
    scan: HistoricalCommunicationScan,
    *,
    secret: bytes,
    research_cache: object,
    existing_topics: Mapping[str, frozenset[str]] | None = None,
    existing_entities: Mapping[str, frozenset[str]] | None = None,
) -> EnrichmentResult:
    """Project only closed researched semantics with recurrence or engagement."""
    existing_topics = existing_topics or {}
    existing_entities = existing_entities or {}
    cache = _cache_results(research_cache, secret=secret)
    signals = _signals(scan)
    record_by_source = {record.bundle.event.source_id: record for record in scan.records}
    positive_records_by_target: dict[str, list[HistoricalCommunicationRecord]] = defaultdict(list)
    for candidate_record in scan.records:
        candidate_event = candidate_record.bundle.event
        if (
            candidate_event.kind is CommunicationKind.REACTION_ADD
            and candidate_event.reaction_subtype in {
                CommunicationReactionSubtype.LIKE, CommunicationReactionSubtype.LOVE,
            }
        ):
            relation = next((
                item for item in candidate_record.bundle.relations
                if item.relation_type is CommunicationRelationType.REACTION_TO
            ), None)
            if relation is not None:
                positive_records_by_target[relation.target_source_id].append(candidate_record)
    raw: dict[tuple[str, str, str], list[DerivedSupport]] = defaultdict(list)
    exclusions: Counter[str] = Counter()
    researched = matched = 0
    for signal in signals:
        eid = evidence_id(secret, "url", signal.identity_url)
        result = cache.get(eid)
        if not result or result.get("status") != "ok":
            exclusions["link_research_unavailable"] += 1
            continue
        researched += 1
        record = record_by_source.get(signal.message_guid)
        if record is None:
            exclusions["link_source_missing"] += 1
            continue
        semantics: list[tuple[str, str, str, str | None]] = []
        raw_ontology = result.get("ontology_ids", ())
        for ontology_id in raw_ontology if isinstance(raw_ontology, (list, tuple)) else ():
            rule = _RULE_BY_ID.get(str(ontology_id))
            if rule is not None:
                semantics.append(("activity" if rule.lane == "activity" else "topic", rule.ontology_id, rule.label, None))
        raw_entities = result.get("public_entities", ())
        for value in raw_entities if isinstance(raw_entities, (list, tuple)) else ():
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                continue
            entity_type, label = str(value[0]), str(value[1])
            if (entity_type, label) in _ENTITY_BY_KEY:
                semantics.append(("entity", f"{entity_type}:{label.casefold()}", label, entity_type))
        if semantics:
            matched += 1
        actors = [(signal.shared_by, "researched_share", False, record)]
        actors.extend(
            (reaction.author, "authenticated_positive_reaction", True, reaction)
            for reaction in positive_records_by_target.get(signal.message_guid, ())
        )
        for kind, semantic_key, label, entity_type in semantics:
            for actor, stance, authenticated, support_record in actors:
                support_event = support_record.bundle.event
                support = DerivedSupport(
                    subject=actor,
                    event_id=support_event.event_id,
                    source_id=support_event.source_id,
                    support_id=_opaque(
                        secret, "phase-e-link-support-v4", actor, support_event.event_id,
                        kind, semantic_key, stance, LINK_DERIVATION_VERSION,
                    ),
                    recurrence_support_id=_opaque(
                        secret, "phase-e-link-recurrence-v4", actor, eid, kind,
                        semantic_key, LINK_DERIVATION_VERSION,
                    ),
                    semantic_kind=kind,
                    semantic_key=semantic_key,
                    canonical_label=label,
                    entity_type=entity_type,
                    actor_code="authenticated_reactor" if authenticated else "authenticated_sharer",
                    polarity="positive" if authenticated else "neutral",
                    stance_code=stance,
                    gate_codes=(),
                    rule_code=f"researched_link:{semantic_key}",
                    occurred_day=int(support_event.occurred_at // 86_400),
                    authenticated_target=authenticated,
                )
                raw[(actor, kind, semantic_key)].append(support)
    candidates: list[EnrichmentCandidate] = []
    watchlist: list[Mapping[str, object]] = []
    for (subject, kind, key), values in sorted(raw.items()):
        supports = tuple(sorted({item.event_id: item for item in values}.values(), key=lambda item: item.event_id))
        days = {item.occurred_day for item in supports}
        positive = [item for item in supports if item.authenticated_target]
        eligible = bool(positive) or len(days) >= 2
        label = supports[0].canonical_label
        if kind in {"topic", "activity"} and label.casefold() in existing_topics.get(subject, frozenset()):
            exclusions["existing_reviewed_topic"] += 1
            continue
        if kind == "entity" and label.casefold() in existing_entities.get(subject, frozenset()):
            exclusions["existing_reviewed_entity"] += 1
            continue
        if eligible:
            candidates.append(EnrichmentCandidate(
                subject=subject, kind=kind, semantic_key=key, label=label,
                entity_type=supports[0].entity_type,
                polarity="positive" if positive else "neutral",
                eligibility="eligible", supports=supports,
                distinct_days=len(days), positive_supports=len(positive),
            ))
        else:
            exclusions["watchlist_neutral_share"] += 1
            watchlist.append({
                "subject": subject, "kind": kind, "semantic_key": key, "label": label,
                "entity_type": supports[0].entity_type, "occurrence_count": len(supports),
                "distinct_days": len(days), "positive_supports": 0,
                "negative_supports": 0, "gated_supports": 0,
                "reason": "neutral_share",
            })
    return EnrichmentResult(
        candidates=tuple(candidates),
        watchlist=tuple(watchlist),
        exclusions=dict(sorted(exclusions.items())),
        metrics={
            "researched_link_occurrences": researched,
            "researched_semantic_link_occurrences": matched,
            "researched_link_candidates": len(candidates),
            "researched_link_watchlist_groups": len(watchlist),
        },
        versions={
            "link_research_version": RESEARCH_VERSION,
            "link_derivation_version": LINK_DERIVATION_VERSION,
            "taxonomy_commitment": TAXONOMY_COMMITMENT,
        },
    )


__all__ = [
    "HistoricalLinkResearch", "LINK_DERIVATION_VERSION", "derive_link_research_enrichment",
    "research_historical_links",
]
