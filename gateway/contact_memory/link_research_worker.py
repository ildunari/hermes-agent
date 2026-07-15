"""Restart-safe private link research worker with no messaging surface."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from typing import Mapping

from .link_research import (
    HermesWebSearchAdapter,
    LinkResearchProvider,
    LinkResearchRequest,
    LinkResearchResult,
    MetadataSearchResearchProvider,
    PinnedHttpsTransport,
)
from .phase_e_taxonomy import PUBLIC_ENTITIES, TAXONOMY
from .private_link_queue import PrivateLinkResearchQueue
from .schema import (
    CommunicationEnrichmentState,
    CommunicationProjection,
    EntityProjection,
    InterestProjection,
    InterestValence,
    ProjectionMethod,
    SignalType,
)
from .store import ContactMemoryStore, normalize_interest_topic

LIVE_LINK_PROJECTOR_VERSION = "phase-e-live-link-v4"
_TRANSIENT = frozenset({"fetch_timeout", "rate_limited", "server_error", "blocked_dns"})
_RULES = {item.ontology_id: item for item in TAXONOMY}
_ENTITY_TYPES = {
    "music_artist": "person", "music_event": "event", "place": "place",
    "vehicle_brand": "thing", "vehicle_model": "thing", "game_platform": "thing",
}
_PUBLIC_ENTITY_BY_KEY = {
    (item.entity_type, item.canonical_label): item for item in PUBLIC_ENTITIES
}


def build_configured_link_research_provider(*, secret: bytes) -> LinkResearchProvider:
    """Use the active Hermes search registry when available, otherwise fetch only."""
    search = None
    try:
        from agent.web_search_registry import get_active_search_provider

        configured = get_active_search_provider()
        if configured is not None and configured.is_available():
            search = HermesWebSearchAdapter(configured, secret=secret)
    except Exception:
        search = None
    return MetadataSearchResearchProvider(
        fetcher=PinnedHttpsTransport(), search_provider=search,
    )


def _payload(result: LinkResearchResult) -> dict[str, object]:
    return {
        "status": result.status,
        "ontology_ids": list(result.ontology_ids),
        "public_entities": [list(item) for item in result.public_entities],
        "source_quality": result.source_quality,
        "recency_band": result.recency_band,
        "support_ids": list(result.support_ids),
        "provider_commitment": result.provider_commitment,
    }


def _aggregate_payloads(payloads: list[dict[str, object]]) -> dict[str, object]:
    ontology: set[str] = set()
    entities: set[tuple[str, str]] = set()
    for payload in payloads:
        if payload.get("status") != "ok":
            continue
        raw_ontology = payload.get("ontology_ids")
        if isinstance(raw_ontology, (list, tuple)):
            ontology.update(str(item) for item in raw_ontology)
        raw_entities = payload.get("public_entities")
        if not isinstance(raw_entities, (list, tuple)):
            continue
        for item in raw_entities:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                entities.add((str(item[0]), str(item[1])))
    return {
        "status": "ok",
        "ontology_ids": sorted(ontology),
        "public_entities": [list(item) for item in sorted(entities)],
    }


def _commitment(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sequence(payload: Mapping[str, object], key: str) -> tuple[object, ...]:
    value = payload.get(key, ())
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _projection(payload: Mapping[str, object], *, engaged: bool) -> CommunicationProjection:
    interests = []
    for raw in _sequence(payload, "ontology_ids"):
        rule = _RULES.get(str(raw))
        if rule is None:
            continue
        interests.append(InterestProjection(
            topic=normalize_interest_topic(rule.label),
            signal_type=SignalType.ENGAGED_MENTION if engaged else SignalType.SPONTANEOUS_RAISE,
            valence=InterestValence.POSITIVE,
            confidence=1.0,
            source_method=ProjectionMethod.DETERMINISTIC,
        ))
    entities = []
    for raw in _sequence(payload, "public_entities"):
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            continue
        public_entity = _PUBLIC_ENTITY_BY_KEY.get((str(raw[0]), str(raw[1])))
        entity_type = _ENTITY_TYPES.get(str(raw[0]))
        if public_entity is None or entity_type is None:
            continue
        entities.append(EntityProjection(
            canonical_label=public_entity.canonical_label, entity_type=entity_type,
            confidence=1.0, source_method=ProjectionMethod.DETERMINISTIC,
        ))
    return CommunicationProjection(interests=tuple(interests), entities=tuple(entities))


def process_one_link_job(
    *,
    root: str | Path,
    contact_id: str,
    provider: LinkResearchProvider,
    now: float | None = None,
) -> dict[str, object]:
    """Claim, research, enrich, and project one job without triggering a reply."""
    timestamp = float(time.time() if now is None else now)
    queue = PrivateLinkResearchQueue(root, contact_id)
    claim = queue.claim_next(now=timestamp, lease_seconds=300.0)
    if claim is None:
        return {"processed": False, "status": "empty"}
    renewed = queue.renew_claim(claim.job_id, claim.claim_token, now=timestamp)
    if renewed is None:
        return {"processed": True, "status": "stale_claim"}
    claim = renewed
    result = provider.research(LinkResearchRequest(
        evidence_id=claim.url_id,
        url=claim.exact_url,
        platform="private-queue",
        shared_by=contact_id,
        occurred_at=claim.occurred_at,
        repeated_shares=claim.repeated_shares,
        distinct_days=claim.distinct_days,
    ))
    refreshed = queue.renew_claim(claim.job_id, claim.claim_token, now=timestamp)
    if refreshed is None:
        return {"processed": True, "status": "stale_claim"}
    claim = refreshed
    if result.status != "ok":
        if result.status in _TRANSIENT:
            queue.retry(claim.job_id, claim.claim_token, failure_code=result.status, now=timestamp)
        else:
            store = ContactMemoryStore(root, contact_id)
            bundle = store.get_communication_bundle(claim.event_id)
            if bundle is not None:
                target = next((item for item in bundle.urls if item.url_id == claim.url_id), None)
                if target is not None:
                    store.enrich_communication_event(
                        claim.event_id,
                        urls=(replace(target, enrichment_state=CommunicationEnrichmentState.UNAVAILABLE),),
                    )
            failure = _commitment({"status": result.status})
            queue.complete(
                claim.job_id, claim.claim_token,
                result_commitment=failure, result_json=json.dumps({"status": result.status}),
                now=timestamp,
            )
        return {"processed": True, "status": result.status}

    payload = _payload(result)
    result_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    store = ContactMemoryStore(root, contact_id)
    bundle = store.get_communication_bundle(claim.event_id)
    if bundle is None:
        queue.retry(claim.job_id, claim.claim_token, failure_code="canonical_event_missing", now=timestamp)
        return {"processed": True, "status": "canonical_event_missing"}
    target = next((item for item in bundle.urls if item.url_id == claim.url_id), None)
    if target is None:
        queue.retry(claim.job_id, claim.claim_token, failure_code="canonical_url_missing", now=timestamp)
        return {"processed": True, "status": "canonical_url_missing"}
    # Recurrence is semantic, not URL-based: distinct links can resolve to the
    # same closed ontology topic or public entity.  This API omits exact URLs
    # and the queue itself is physically isolated per contact.
    prior = queue.completed_results()
    payload_by_event: dict[str, dict[str, object]] = {}
    for row in prior:
        try:
            prior_payload = json.loads(str(row["result_json"] or ""))
        except json.JSONDecodeError:
            continue
        if prior_payload.get("status") == "ok":
            payload_by_event[str(row["event_id"])] = prior_payload
    payload_by_event[claim.event_id] = payload

    topic_events: dict[str, set[str]] = {}
    entity_events: dict[tuple[str, str], set[str]] = {}
    for event_id, event_payload in payload_by_event.items():
        for ontology_id in _sequence(event_payload, "ontology_ids"):
            topic_events.setdefault(str(ontology_id), set()).add(event_id)
        for raw_entity in _sequence(event_payload, "public_entities"):
            if isinstance(raw_entity, (list, tuple)) and len(raw_entity) == 2:
                entity_events.setdefault(
                    (str(raw_entity[0]), str(raw_entity[1])), set()
                ).add(event_id)

    engaged = claim.engagement_score > 0
    admitted_topics = {
        key for key, event_ids in topic_events.items()
        if len(event_ids) >= 2 or (engaged and claim.event_id in event_ids)
    }
    admitted_entities = {
        key for key, event_ids in entity_events.items()
        if len(event_ids) >= 2 or (engaged and claim.event_id in event_ids)
    }
    recurrent = any(len(topic_events[key]) >= 2 for key in admitted_topics) or any(
        len(entity_events[key]) >= 2 for key in admitted_entities
    )
    admitted_payload = {
        "ontology_ids": sorted(admitted_topics),
        "public_entities": [list(item) for item in sorted(admitted_entities)],
    }
    semantic_commitment = _commitment(admitted_payload)
    result_commitment = _commitment(payload)
    projector_version = (
        f"{LIVE_LINK_PROJECTOR_VERSION}.r.{semantic_commitment[:32]}"
        if recurrent else
        f"{LIVE_LINK_PROJECTOR_VERSION}.e.{claim.url_id[:32]}.{claim.projection_generation}"
    )
    projections: list[tuple[str, CommunicationProjection]] = []
    for event_id, event_payload in sorted(payload_by_event.items()):
        filtered = {
            "ontology_ids": [
                item for item in _sequence(event_payload, "ontology_ids")
                if str(item) in admitted_topics
            ],
            "public_entities": [
                item for item in _sequence(event_payload, "public_entities")
                if isinstance(item, (list, tuple)) and len(item) == 2
                and (str(item[0]), str(item[1])) in admitted_entities
            ],
        }
        projection = _projection(
            filtered, engaged=engaged and event_id == claim.event_id and not recurrent,
        )
        if projection.interests or projection.entities:
            projections.append((event_id, projection))
    projected_event_ids: tuple[str, ...] = ()
    enrichment_applied = False
    claim_event_id = claim.event_id
    projected = 0

    def compensate_attempt() -> None:
        """Undo only canonical mutations newly owned by this claim attempt."""
        for event_id in projected_event_ids:
            store.deactivate_projector_event(
                event_id, projector_version=projector_version, now=timestamp,
            )
        if enrichment_applied:
            store.compensate_communication_url_review(claim_event_id, target.url_id)

    try:
        store.enrich_communication_event(
            claim.event_id,
            urls=(replace(target, enrichment_state=CommunicationEnrichmentState.REVIEWED),),
        )
        enrichment_applied = (
            target.enrichment_state is CommunicationEnrichmentState.PENDING
        )
        refreshed = queue.renew_claim(claim.job_id, claim.claim_token, now=timestamp)
        if refreshed is None:
            compensate_attempt()
            return {"processed": True, "status": "stale_claim"}
        claim = refreshed
        if projections:
            projection_results = store.project_communication_events(
                projections, projector_version=projector_version, now=timestamp,
            )
            # Stable recurrent versions can already have successful receipts.
            # Compensate only receipts inserted by this attempt.
            projected_event_ids = tuple(
                item.event_id for item in projection_results if item.inserted
            )
            projected = len(projection_results)
        refreshed = queue.renew_claim(claim.job_id, claim.claim_token, now=timestamp)
        if refreshed is None:
            compensate_attempt()
            return {"processed": True, "status": "stale_claim"}
        if not recurrent and refreshed.engagement_score <= 0:
            for event_id in projected_event_ids:
                store.deactivate_projector_event(
                    event_id, projector_version=projector_version, now=timestamp,
                )
            projected = 0
        try:
            queue.complete(
                claim.job_id, claim.claim_token,
                result_commitment=result_commitment, result_json=result_json, now=timestamp,
            )
        except ValueError:
            compensate_attempt()
            return {"processed": True, "status": "stale_claim"}
    except BaseException:
        compensate_attempt()
        refreshed = queue.renew_claim(claim.job_id, claim.claim_token, now=timestamp)
        if refreshed is not None:
            queue.retry(
                claim.job_id, claim.claim_token,
                failure_code="canonical_apply_failed", now=timestamp,
            )
        raise
    return {"processed": True, "status": "ok", "projected_events": projected}


__all__ = [
    "LIVE_LINK_PROJECTOR_VERSION", "build_configured_link_research_provider",
    "process_one_link_job",
]
