"""Reviewed Phase E projection preparation over canonical historical evidence."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from typing import Any, Mapping, Sequence

from .imessage_communication_adapter import (
    HistoricalCommunicationScan,
    build_aggregate_manifest,
)
from .imessage_link_review import _metadata_topics, evidence_id, sanitize_metadata_cache
from .schema import (
    CommunicationBundle,
    CallbackProjection,
    CommunicationKind,
    CommunicationProjection,
    CommunicationRecommendationOutcome,
    CommunicationRelationType,
    EntityProjection,
    InterestProjection,
    InterestValence,
    ProjectionMethod,
    RecommendationProjection,
    SignalType,
)
from .store import ContactMemoryStore, normalize_interest_topic, opaque_contact_filename

_SUBJECTS = ("kosta-owner", "stephen-lucier")
_PROJECTOR_VERSION = "phase-e-reviewed-v2"
_RETIRED_REVIEW_IDS = frozenset({
    "8ce521b7cde167ffc581f6b1b639895de84f46b955e42affd51b72041b2aafb1",
})
_PLATFORM_LABELS = {
    "instagram": "Instagram",
    "spotify": "Spotify",
    "tiktok": "TikTok",
    "x": "X",
    "youtube": "YouTube",
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _typed_payload(value: object) -> object:
    """Serialize complete validated dataclasses without dropping persisted fields."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _typed_payload(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, (tuple, list)):
        return [_typed_payload(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _typed_payload(item) for key, item in value.items()}
    return value


def _projection_payload(projection: CommunicationProjection) -> dict[str, object]:
    payload = _typed_payload(projection)
    assert isinstance(payload, dict)
    return payload


def _projection_from_payload(payload: object) -> CommunicationProjection:
    if not isinstance(payload, Mapping) or set(payload) != {
        "interests", "entities", "recommendations", "callbacks",
    }:
        raise ValueError("signed projection payload has invalid fields")
    try:
        interests = tuple(InterestProjection(
            topic=item["topic"], signal_type=SignalType(item["signal_type"]),
            valence=InterestValence(item["valence"]), confidence=item["confidence"],
            source_method=ProjectionMethod(item["source_method"]),
        ) for item in payload["interests"])
        entities = tuple(EntityProjection(
            canonical_label=item["canonical_label"], entity_type=item["entity_type"],
            confidence=item["confidence"],
            source_method=ProjectionMethod(item["source_method"]),
        ) for item in payload["entities"])
        recommendations = tuple(RecommendationProjection(
            semantic_key=item["semantic_key"],
            outcome=CommunicationRecommendationOutcome(item["outcome"]),
            confidence=item["confidence"],
            source_method=ProjectionMethod(item["source_method"]),
            explicit_linkage=item["explicit_linkage"], topic=item["topic"],
            recommendation=item["recommendation"],
        ) for item in payload["recommendations"])
        callbacks = tuple(CallbackProjection(
            semantic_key=item["semantic_key"], canonical_label=item["canonical_label"],
            confidence=item["confidence"],
            source_method=ProjectionMethod(item["source_method"]),
            supporting_event_ids=tuple(item["supporting_event_ids"]),
        ) for item in payload["callbacks"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("signed projection payload is invalid") from exc
    projection = CommunicationProjection(
        interests=interests, entities=entities,
        recommendations=recommendations, callbacks=callbacks,
    )
    if _projection_payload(projection) != payload:
        raise ValueError("signed projection payload is not canonical")
    return projection


@dataclass(frozen=True)
class ExistingReviewedState:
    topics: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    recommendation_keys: frozenset[str] = frozenset()
    callback_keys: frozenset[str] = frozenset()
    event_ids: frozenset[str] = frozenset()
    reviewed_import_runs: int = 0


@dataclass(frozen=True)
class ReviewedBackfill:
    manifest: dict[str, Any]


def _existing_state_payload(state: ExistingReviewedState) -> dict[str, object]:
    return {
        "topics": sorted(state.topics), "entities": sorted(state.entities),
        "recommendation_keys": sorted(state.recommendation_keys),
        "callback_keys": sorted(state.callback_keys), "event_ids": sorted(state.event_ids),
        "reviewed_import_runs": state.reviewed_import_runs,
    }


def read_existing_reviewed_state(root: str | Path, subject: str) -> ExistingReviewedState:
    """Inspect a live namespace through a WAL-aware read-only connection."""
    if subject not in _SUBJECTS:
        raise ValueError("unsupported reviewed backfill subject")
    path = Path(root).expanduser().resolve() / "contacts" / opaque_contact_filename(subject)
    if not path.is_file():
        return ExistingReviewedState()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        topics = {
            normalize_interest_topic(row[0])
            for row in con.execute("SELECT topic FROM interest")
        } if "interest" in tables else set()
        if "import_run" in tables:
            imports = list(con.execute("SELECT run_id,manifest_json FROM import_run"))
            for _run_id, raw_manifest in imports:
                try:
                    manifest = json.loads(raw_manifest)
                except (TypeError, json.JSONDecodeError):
                    continue
                for item in manifest.get("events", ()) if isinstance(manifest, dict) else ():
                    topic = item.get("topic") if isinstance(item, dict) else None
                    if isinstance(topic, str):
                        try:
                            topics.add(normalize_interest_topic(topic))
                        except ValueError:
                            pass
            reviewed_runs = sum(
                str(run_id).startswith(("reviewed-", "reviewed_"))
                for run_id, _manifest in imports
            )
        else:
            reviewed_runs = 0
        entities = {
            " ".join(str(row[0]).casefold().split())
            for row in con.execute("SELECT canonical_label FROM projected_entity WHERE active=1")
        } if "projected_entity" in tables else set()
        recommendations = {
            str(row[0]) for row in con.execute(
                "SELECT semantic_key FROM projected_recommendation WHERE active=1"
            )
        } if "projected_recommendation" in tables else set()
        callbacks = {
            str(row[0]) for row in con.execute(
                "SELECT semantic_key FROM semantic_callback WHERE active=1"
            )
        } if "semantic_callback" in tables else set()
        event_ids = {
            str(row[0]) for row in con.execute("SELECT event_id FROM communication_event")
        } if "communication_event" in tables else set()
    finally:
        con.close()
    return ExistingReviewedState(
        topics=frozenset(topics), entities=frozenset(entities),
        recommendation_keys=frozenset(recommendations),
        callback_keys=frozenset(callbacks), event_ids=frozenset(event_ids),
        reviewed_import_runs=reviewed_runs,
    )


def _topic_for_text(text: object) -> str | None:
    raw = str(text or "")
    # URLs are evidence for the bounded metadata lane, not semantic words.
    visible = re.sub(r"https?://\S+", " ", raw)
    matches = _metadata_topics({"title": visible})
    if not matches:
        return None
    return normalize_interest_topic(matches[0][1].replace(",", ""))


def _projection_with(
    prior: CommunicationProjection | None,
    *,
    interest: InterestProjection | None = None,
    entity: EntityProjection | None = None,
) -> CommunicationProjection:
    current = prior or CommunicationProjection()
    return CommunicationProjection(
        interests=(*current.interests, *((interest,) if interest is not None else ())),
        entities=(*current.entities, *((entity,) if entity is not None else ())),
        recommendations=current.recommendations,
        callbacks=current.callbacks,
    )


def build_reviewed_backfill(
    scan: HistoricalCommunicationScan,
    *,
    secret: bytes,
    existing: Mapping[str, ExistingReviewedState] | None = None,
    metadata_cache: object | None = None,
) -> ReviewedBackfill:
    """Build a signed aggregate candidate manifest and typed Phase D proposals."""
    if len(secret) < 16:
        raise ValueError("HMAC secret must be at least 16 bytes")
    states = {
        subject: (existing or {}).get(subject, ExistingReviewedState())
        for subject in _SUBJECTS
    }
    metadata = sanitize_metadata_cache(metadata_cache or {})
    exclusions: Counter[str] = Counter()
    topic_events: dict[tuple[str, str], list[str]] = defaultdict(list)
    topic_days: dict[tuple[str, str], set[int]] = defaultdict(set)
    entity_events: dict[tuple[str, str], list[str]] = defaultdict(list)
    by_event = {record.bundle.event.event_id: record for record in scan.records}

    for record in scan.records:
        event = record.bundle.event
        if event.event_id in states[record.author].event_ids:
            exclusions["existing_canonical_event"] += 1
            continue
        relations = {relation.relation_type for relation in record.bundle.relations}
        topic: str | None = None
        if event.kind is CommunicationKind.TEXT and not relations.intersection({
            CommunicationRelationType.REPLY_TO, CommunicationRelationType.BATCH_MEMBER_OF,
        }):
            topic = _topic_for_text(record.private_evidence.get("text"))
        elif event.kind is CommunicationKind.LINK_SHARE:
            matches: list[tuple[str, str]] = []
            for raw_url in record.private_evidence.get("urls", ()):
                identity = evidence_id(secret, "url", str(raw_url))
                matches.extend(_metadata_topics(metadata.get(identity, {})))
            if matches:
                topic = normalize_interest_topic(matches[0][1].replace(",", ""))
        if topic is not None:
            key = (record.author, topic)
            topic_events[key].append(event.event_id)
            topic_days[key].add(int(event.occurred_at // 86_400))
        for item in record.bundle.urls:
            label = _PLATFORM_LABELS.get(item.platform or "")
            if label is not None:
                entity_events[(record.author, label)].append(event.event_id)

    candidates: list[dict[str, Any]] = []
    by_subject = {
        subject: {"topics": 0, "entities": 0, "recommendations": 0, "callbacks": 0}
        for subject in _SUBJECTS
    }

    for (subject, topic), raw_ids in sorted(topic_events.items()):
        event_ids = tuple(dict.fromkeys(raw_ids))
        if len(event_ids) < 2 or len(topic_days[(subject, topic)]) < 2:
            exclusions["insufficient_topic_recurrence"] += 1
            continue
        if topic in states[subject].topics:
            exclusions["existing_reviewed_topic"] += 1
            continue
        occurrences = []
        for event_id in event_ids:
            bundle = by_event[event_id].bundle
            projection = CommunicationProjection(interests=(InterestProjection(
                topic=topic, signal_type=SignalType.SPONTANEOUS_RAISE,
                valence=InterestValence.POSITIVE, confidence=1.0,
                source_method=ProjectionMethod.DETERMINISTIC,
            ),))
            occurrences.append({
                "event_id": event_id, "source_id": bundle.event.source_id,
                "bundle_commitment": _sha256(_canonical_bytes(_typed_payload(bundle))),
                "projection": _projection_payload(projection),
            })
        candidate_body = {
            "subject": subject, "kind": "topic",
            "semantic_key": topic, "label": topic,
            "occurrence_count": len(event_ids), "distinct_days": len(topic_days[(subject, topic)]),
            "occurrences": occurrences,
        }
        candidates.append({
            "candidate_id": evidence_id(
                secret, "phase-e-candidate-v2",
                _canonical_bytes(candidate_body).decode("utf-8"),
            ), **candidate_body,
        })
        by_subject[subject]["topics"] += 1

    for (subject, label), raw_ids in sorted(entity_events.items()):
        event_ids = tuple(dict.fromkeys(raw_ids))
        if len(event_ids) < 2:
            exclusions["insufficient_entity_recurrence"] += 1
            continue
        normalized = " ".join(label.casefold().split())
        if normalized in states[subject].entities:
            exclusions["existing_reviewed_entity"] += 1
            continue
        occurrences = []
        for event_id in event_ids:
            bundle = by_event[event_id].bundle
            projection = CommunicationProjection(entities=(EntityProjection(
                canonical_label=label, entity_type="organization", confidence=1.0,
                source_method=ProjectionMethod.DETERMINISTIC,
            ),))
            occurrences.append({
                "event_id": event_id, "source_id": bundle.event.source_id,
                "bundle_commitment": _sha256(_canonical_bytes(_typed_payload(bundle))),
                "projection": _projection_payload(projection),
            })
        candidate_body = {
            "subject": subject, "kind": "entity",
            "semantic_key": normalized, "label": label,
            "occurrence_count": len(event_ids), "distinct_days": len({
                int(by_event[event_id].bundle.event.occurred_at // 86_400)
                for event_id in event_ids
            }), "occurrences": occurrences,
        }
        candidates.append({
            "candidate_id": evidence_id(
                secret, "phase-e-candidate-v2",
                _canonical_bytes(candidate_body).decode("utf-8"),
            ), **candidate_body,
        })
        by_subject[subject]["entities"] += 1

    # Historical Phase B events do not mechanically establish recommendation or
    # callback semantics. Keep those lanes explicit and empty rather than guessing.
    exclusions["recommendation_requires_typed_linkage"] += sum(
        record.bundle.event.kind is CommunicationKind.TEXT for record in scan.records
    )
    exclusions["callback_requires_reviewed_recurrence"] += sum(
        record.bundle.event.kind is CommunicationKind.TEXT for record in scan.records
    )
    candidates.sort(key=lambda item: (
        item["subject"], item["kind"], item["semantic_key"], item["candidate_id"],
    ))
    phase_b = build_aggregate_manifest(scan, secret=secret)
    subject_reviews: list[dict[str, Any]] = []
    for subject in _SUBJECTS:
        subject_candidates = [item for item in candidates if item["subject"] == subject]
        source_records = [
            _typed_payload(record.bundle) for record in scan.records if record.author == subject
        ]
        target_snapshot = _existing_state_payload(states[subject])
        subject_unsigned: dict[str, Any] = {
            "schema": 2,
            "kind": "reviewed-canonical-communication-backfill-subject",
            "subject": subject, "projector_version": _PROJECTOR_VERSION,
            "source_snapshot_commitment": evidence_id(
                secret, "phase-e-subject-source-v2",
                _canonical_bytes(source_records).decode("utf-8"),
            ),
            "target_snapshot": target_snapshot,
            "target_snapshot_commitment": evidence_id(
                secret, "phase-e-subject-target-v2",
                _canonical_bytes({"subject": subject, "state": target_snapshot}).decode("utf-8"),
            ),
            "candidates": subject_candidates,
            "counts": {
                "candidates": len(subject_candidates),
                "projection_events": len({
                    occurrence["event_id"] for candidate in subject_candidates
                    for occurrence in candidate["occurrences"]
                }),
            },
        }
        subject_unsigned["subject_review_id"] = evidence_id(
            secret, "phase-e-subject-review-v2",
            _canonical_bytes(subject_unsigned).decode("utf-8"),
        )
        subject_reviews.append(subject_unsigned)

    unsigned: dict[str, Any] = {
        "schema": 2,
        "kind": "reviewed-canonical-communication-backfill-package",
        "apply_supported": True,
        "atomic_unit": "subject",
        "projector_version": _PROJECTOR_VERSION,
        "source_review_id": phase_b["review_id"],
        "evidence_commitment": phase_b["evidence_commitment"],
        "accounting": dict(scan.accounting),
        "candidates": candidates,
        "subject_reviews": subject_reviews,
        "counts": {
            "candidate_total": len(candidates),
            "by_subject": by_subject,
            "projection_events": {
                item["subject"]: item["counts"]["projection_events"]
                for item in subject_reviews
            },
            "existing_reviewed_import_runs": {
                subject: states[subject].reviewed_import_runs for subject in _SUBJECTS
            },
        },
        "exclusions": dict(sorted(exclusions.items())),
        "approval_boundary": {
            "requires_exact_subject_review_id": True,
            "requires_subject": True,
            "requires_explicit_candidate_ids": True,
            "cross_subject_apply": False,
        },
    }
    unsigned["global_review_id"] = evidence_id(
        secret, "phase-e-global-review-v2", _canonical_bytes(unsigned).decode("utf-8"),
    )
    return ReviewedBackfill(manifest=unsigned)


def verify_review_manifest(manifest: Mapping[str, Any], *, secret: bytes) -> bool:
    supplied = manifest.get("global_review_id")
    if not isinstance(supplied, str) or re.fullmatch(r"[0-9a-f]{64}", supplied) is None:
        return False
    if supplied in _RETIRED_REVIEW_IDS:
        return False
    unsigned = dict(manifest)
    unsigned.pop("global_review_id", None)
    expected = evidence_id(
        secret, "phase-e-global-review-v2", _canonical_bytes(unsigned).decode("utf-8"),
    )
    if not hmac.compare_digest(supplied, expected):
        return False
    reviews = manifest.get("subject_reviews")
    return isinstance(reviews, list) and len(reviews) == len(_SUBJECTS) and all(
        verify_subject_review(item, secret=secret) for item in reviews
    )


def verify_subject_review(manifest: Mapping[str, Any], *, secret: bytes) -> bool:
    supplied = manifest.get("subject_review_id")
    if (
        manifest.get("schema") != 2
        or manifest.get("kind") != "reviewed-canonical-communication-backfill-subject"
        or not isinstance(supplied, str)
        or re.fullmatch(r"[0-9a-f]{64}", supplied) is None
        or supplied in _RETIRED_REVIEW_IDS
    ):
        return False
    unsigned = dict(manifest)
    unsigned.pop("subject_review_id", None)
    expected = evidence_id(
        secret, "phase-e-subject-review-v2",
        _canonical_bytes(unsigned).decode("utf-8"),
    )
    return hmac.compare_digest(supplied, expected)


def subject_review(review: ReviewedBackfill, subject: str) -> dict[str, Any]:
    matches = [
        item for item in review.manifest.get("subject_reviews", ())
        if isinstance(item, dict) and item.get("subject") == subject
    ]
    if len(matches) != 1:
        raise ValueError("review package lacks exactly one subject snapshot")
    return matches[0]


def _merge_projection(
    prior: CommunicationProjection | None,
    addition: CommunicationProjection,
) -> CommunicationProjection:
    current = prior or CommunicationProjection()
    return CommunicationProjection(
        interests=(*current.interests, *addition.interests),
        entities=(*current.entities, *addition.entities),
        recommendations=(*current.recommendations, *addition.recommendations),
        callbacks=(*current.callbacks, *addition.callbacks),
    )


def apply_subject_backfill(
    scan: HistoricalCommunicationScan,
    review: ReviewedBackfill,
    *,
    subject: str,
    store: ContactMemoryStore,
    approved_subject_review_id: str,
    approved_candidate_ids: Sequence[str],
    secret: bytes,
) -> dict[str, object]:
    """Apply exactly one approved subject through one SQLite transaction."""
    if subject not in _SUBJECTS:
        raise ValueError("unsupported reviewed backfill subject")
    if store.contact_id != subject:
        raise ValueError("subject does not match the physical contact namespace")
    if approved_subject_review_id in _RETIRED_REVIEW_IDS:
        raise ValueError("approved review ID has been retired")
    target_review = subject_review(review, subject)
    if approved_subject_review_id != target_review.get("subject_review_id"):
        raise ValueError("approved subject review ID must exactly match the signed snapshot")
    if not verify_subject_review(target_review, secret=secret):
        raise ValueError("subject review HMAC verification failed")
    requested = tuple(approved_candidate_ids)
    if not requested or len(set(requested)) != len(requested):
        raise ValueError("approved candidate IDs must be a non-empty duplicate-free allowlist")
    candidates = target_review.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("subject review candidates are invalid")
    by_candidate = {item.get("candidate_id"): item for item in candidates if isinstance(item, Mapping)}
    if len(by_candidate) != len(candidates) or any(item not in by_candidate for item in requested):
        raise ValueError("approved candidate ID is unknown or belongs to another subject")

    source_records = [
        _typed_payload(record.bundle) for record in scan.records if record.author == subject
    ]
    current_source = evidence_id(
        secret, "phase-e-subject-source-v2",
        _canonical_bytes(source_records).decode("utf-8"),
    )
    if current_source != target_review.get("source_snapshot_commitment"):
        raise ValueError("subject deterministic source scan is stale")
    scan_by_event = {
        record.bundle.event.event_id: record.bundle
        for record in scan.records if record.author == subject
    }
    selected_candidates = [by_candidate[item] for item in sorted(requested)]
    projections: dict[str, CommunicationProjection] = {}
    event_ids: set[str] = set()
    for candidate in selected_candidates:
        body = dict(candidate)
        supplied_candidate_id = body.pop("candidate_id", None)
        expected_candidate_id = evidence_id(
            secret, "phase-e-candidate-v2", _canonical_bytes(body).decode("utf-8"),
        )
        if not isinstance(supplied_candidate_id, str) or not hmac.compare_digest(
            supplied_candidate_id, expected_candidate_id,
        ):
            raise ValueError("signed candidate payload is invalid")
        if candidate.get("subject") != subject:
            raise ValueError("approved candidate crosses subject boundary")
        occurrences = candidate.get("occurrences")
        if not isinstance(occurrences, list) or len(occurrences) != candidate.get("occurrence_count"):
            raise ValueError("signed candidate occurrence accounting is invalid")
        for occurrence in occurrences:
            if not isinstance(occurrence, Mapping):
                raise ValueError("signed candidate occurrence is invalid")
            event_id = occurrence.get("event_id")
            bundle = scan_by_event.get(event_id)
            if bundle is None or occurrence.get("source_id") != bundle.event.source_id:
                raise ValueError("reviewed projection evidence is incomplete or cross-subject")
            commitment = _sha256(_canonical_bytes(_typed_payload(bundle)))
            if occurrence.get("bundle_commitment") != commitment:
                raise ValueError("signed canonical occurrence evidence changed")
            projection = _projection_from_payload(occurrence.get("projection"))
            projections[event_id] = _merge_projection(projections.get(event_id), projection)
            event_ids.add(event_id)
    bundles = [scan_by_event[event_id] for event_id in event_ids]
    supplied = tuple(sorted(projections.items()))
    approval_id = evidence_id(
        secret, "phase-e-approval-v2",
        _canonical_bytes({
            "subject": subject, "subject_review_id": approved_subject_review_id,
            "candidate_ids": sorted(requested),
        }).decode("utf-8"),
    )
    target_manifest = {
        "schema": 2, "kind": "reviewed-canonical-communication-approved-subset",
        "subject": subject, "subject_review_id": approved_subject_review_id,
        "approval_id": approval_id, "projector_version": target_review["projector_version"],
        "target_snapshot_commitment": target_review["target_snapshot_commitment"],
        "candidate_ids": sorted(requested), "candidates": selected_candidates,
    }
    source_hash = _sha256(_canonical_bytes(target_manifest))
    result = store.import_reviewed_communication_backfill(
        bundles, supplied,
        projector_version=str(target_review["projector_version"]),
        run_id=f"reviewed-communication:{approval_id}:{subject}",
        source_hash=f"{source_hash}:{subject}", manifest=target_manifest,
        expected_target_snapshot=target_review["target_snapshot"],
    )
    return {
        **result, "approval_id": approval_id,
        "projected_candidates": len(selected_candidates),
    }


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_con = sqlite3.connect(destination)
    try:
        source_con.backup(destination_con)
    finally:
        destination_con.close()
        source_con.close()
    os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)


def restore_rehearsal(
    scan: HistoricalCommunicationScan,
    review: ReviewedBackfill,
    *,
    subject: str,
    source_store: ContactMemoryStore | str | Path,
    rehearsal_root: str | Path,
    secret: bytes,
    approved_candidate_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Exercise backup, apply, and byte-exact restore only in a temporary root."""
    root = Path(rehearsal_root).expanduser().resolve()
    if root.exists():
        raise ValueError("restore rehearsal root must not already exist")
    root.mkdir(parents=True, mode=0o700)
    backup = root / "backup.sqlite3"
    working_root = root / "working"
    working = working_root / "contacts" / opaque_contact_filename(subject)
    source_path = (
        source_store.path if isinstance(source_store, ContactMemoryStore)
        else Path(source_store).expanduser().resolve()
    )
    _sqlite_backup(source_path, backup)
    _sqlite_backup(source_path, working)
    backup_sha = _sha256(backup.read_bytes())
    rehearsal_store = ContactMemoryStore(working_root, subject)
    target_review = subject_review(review, subject)
    candidate_ids = tuple(approved_candidate_ids or (
        item["candidate_id"] for item in target_review["candidates"]
    ))
    applied = apply_subject_backfill(
        scan, review, subject=subject, store=rehearsal_store,
        approved_subject_review_id=str(target_review["subject_review_id"]),
        approved_candidate_ids=candidate_ids, secret=secret,
    )
    mutated_snapshot = root / "mutated-snapshot.sqlite3"
    _sqlite_backup(working, mutated_snapshot)
    mutated_sha = _sha256(mutated_snapshot.read_bytes())
    for suffix in ("-wal", "-shm"):
        Path(str(working) + suffix).unlink(missing_ok=True)
    shutil.copyfile(backup, working)
    os.chmod(working, stat.S_IRUSR | stat.S_IWUSR)
    restored_sha = _sha256(working.read_bytes())
    if restored_sha != backup_sha:
        raise RuntimeError("restore rehearsal did not reproduce the backup bytes")
    if mutated_sha == backup_sha and applied["projected_events"]:
        raise RuntimeError("restore rehearsal did not observe the staged mutation")
    return {
        "subject": subject,
        "backup_sha256": backup_sha,
        "mutated_sha256": mutated_sha,
        "restored_sha256": restored_sha,
        "restore_exact": True,
        "apply_projected_candidates": int(applied["projected_candidates"]),
    }


__all__ = [
    "ExistingReviewedState", "ReviewedBackfill", "apply_subject_backfill",
    "build_reviewed_backfill", "read_existing_reviewed_state", "restore_rehearsal",
    "subject_review", "verify_review_manifest", "verify_subject_review",
]
