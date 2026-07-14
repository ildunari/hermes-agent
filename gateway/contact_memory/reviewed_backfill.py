"""Reviewed Phase E projection preparation over canonical historical evidence."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
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
    CommunicationKind,
    CommunicationProjection,
    CommunicationRelationType,
    EntityProjection,
    InterestProjection,
    InterestValence,
    ProjectionMethod,
    SignalType,
)
from .store import ContactMemoryStore, normalize_interest_topic, opaque_contact_filename

_SUBJECTS = ("kosta-owner", "stephen-lucier")
_PROJECTOR_VERSION = "phase-e-reviewed-v1"
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
    projections: Mapping[str, tuple[tuple[str, CommunicationProjection], ...]]


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

    projections: dict[str, dict[str, CommunicationProjection]] = {
        subject: {} for subject in _SUBJECTS
    }
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
        candidate_id = evidence_id(secret, "phase-e-candidate", "\0".join((
            subject, "topic", topic, *event_ids,
        )))
        candidates.append({
            "candidate_id": candidate_id, "subject": subject, "kind": "topic",
            "semantic_key": topic, "label": topic,
            "occurrence_count": len(event_ids), "distinct_days": len(topic_days[(subject, topic)]),
            "occurrence_event_ids": list(event_ids),
        })
        by_subject[subject]["topics"] += 1
        for event_id in event_ids:
            event = by_event[event_id].bundle.event
            signal = SignalType.SPONTANEOUS_RAISE
            proposal = InterestProjection(
                topic=topic, signal_type=signal, valence=InterestValence.POSITIVE,
                confidence=1.0, source_method=ProjectionMethod.DETERMINISTIC,
            )
            projections[subject][event_id] = _projection_with(
                projections[subject].get(event_id), interest=proposal,
            )

    for (subject, label), raw_ids in sorted(entity_events.items()):
        event_ids = tuple(dict.fromkeys(raw_ids))
        if len(event_ids) < 2:
            exclusions["insufficient_entity_recurrence"] += 1
            continue
        normalized = " ".join(label.casefold().split())
        if normalized in states[subject].entities:
            exclusions["existing_reviewed_entity"] += 1
            continue
        candidate_id = evidence_id(secret, "phase-e-candidate", "\0".join((
            subject, "entity", normalized, *event_ids,
        )))
        candidates.append({
            "candidate_id": candidate_id, "subject": subject, "kind": "entity",
            "semantic_key": normalized, "label": label,
            "occurrence_count": len(event_ids), "distinct_days": len({
                int(by_event[event_id].bundle.event.occurred_at // 86_400)
                for event_id in event_ids
            }), "occurrence_event_ids": list(event_ids),
        })
        by_subject[subject]["entities"] += 1
        for event_id in event_ids:
            proposal = EntityProjection(
                canonical_label=label, entity_type="organization", confidence=1.0,
                source_method=ProjectionMethod.DETERMINISTIC,
            )
            projections[subject][event_id] = _projection_with(
                projections[subject].get(event_id), entity=proposal,
            )

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
    unsigned: dict[str, Any] = {
        "schema": 1,
        "kind": "reviewed-canonical-communication-backfill",
        "apply_supported": True,
        "atomic_unit": "subject",
        "projector_version": _PROJECTOR_VERSION,
        "source_review_id": phase_b["review_id"],
        "evidence_commitment": phase_b["evidence_commitment"],
        "accounting": dict(scan.accounting),
        "candidates": candidates,
        "counts": {
            "candidate_total": len(candidates),
            "by_subject": by_subject,
            "projection_events": {
                subject: len(projections[subject]) for subject in _SUBJECTS
            },
            "existing_reviewed_import_runs": {
                subject: states[subject].reviewed_import_runs for subject in _SUBJECTS
            },
        },
        "exclusions": dict(sorted(exclusions.items())),
        "approval_boundary": {
            "requires_exact_review_id": True,
            "requires_subject": True,
            "cross_subject_apply": False,
        },
    }
    unsigned["review_id"] = evidence_id(
        secret, "phase-e-review", _canonical_bytes(unsigned).decode("utf-8"),
    )
    return ReviewedBackfill(
        manifest=unsigned,
        projections={
            subject: tuple(sorted(projections[subject].items())) for subject in _SUBJECTS
        },
    )


def verify_review_manifest(manifest: Mapping[str, Any], *, secret: bytes) -> bool:
    supplied = manifest.get("review_id")
    if not isinstance(supplied, str) or re.fullmatch(r"[0-9a-f]{64}", supplied) is None:
        return False
    unsigned = dict(manifest)
    unsigned.pop("review_id", None)
    expected = evidence_id(
        secret, "phase-e-review", _canonical_bytes(unsigned).decode("utf-8"),
    )
    return hmac.compare_digest(supplied, expected)


def _subject_manifest(review: ReviewedBackfill, subject: str) -> dict[str, Any]:
    candidates = [
        item for item in review.manifest["candidates"] if item["subject"] == subject
    ]
    return {
        "schema": review.manifest["schema"],
        "kind": "reviewed-canonical-communication-backfill-subject",
        "approved_global_review_id": review.manifest["review_id"],
        "subject": subject,
        "projector_version": review.manifest["projector_version"],
        "candidates": candidates,
        "counts": {
            "candidates": len(candidates),
            "projection_events": len(review.projections[subject]),
        },
    }


def apply_subject_backfill(
    scan: HistoricalCommunicationScan,
    review: ReviewedBackfill,
    *,
    subject: str,
    store: ContactMemoryStore,
    approved_review_id: str,
    secret: bytes,
) -> dict[str, object]:
    """Apply exactly one approved subject through one SQLite transaction."""
    if subject not in _SUBJECTS:
        raise ValueError("unsupported reviewed backfill subject")
    if store.contact_id != subject:
        raise ValueError("subject does not match the physical contact namespace")
    if approved_review_id != review.manifest.get("review_id"):
        raise ValueError("approved review ID must exactly match the signed manifest")
    if not verify_review_manifest(review.manifest, secret=secret):
        raise ValueError("review manifest HMAC verification failed")
    event_ids = {event_id for event_id, _projection in review.projections[subject]}
    bundles = [
        record.bundle for record in scan.records
        if record.author == subject and record.bundle.event.event_id in event_ids
    ]
    if {bundle.event.event_id for bundle in bundles} != event_ids:
        raise ValueError("reviewed projection evidence is incomplete or cross-subject")
    target_manifest = _subject_manifest(review, subject)
    source_hash = _sha256(_canonical_bytes(target_manifest))
    result = store.import_reviewed_communication_backfill(
        bundles, review.projections[subject],
        projector_version=str(review.manifest["projector_version"]),
        run_id=f"reviewed-communication:{review.manifest['review_id']}:{subject}",
        source_hash=f"{source_hash}:{subject}", manifest=target_manifest,
    )
    return {**result, "projected_candidates": len(target_manifest["candidates"])}


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
    applied = apply_subject_backfill(
        scan, review, subject=subject, store=rehearsal_store,
        approved_review_id=str(review.manifest["review_id"]), secret=secret,
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
    "verify_review_manifest",
]
