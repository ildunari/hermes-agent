#!/usr/bin/env python3
"""Validate and explicitly apply an approved aggregate link-interest review.

Dry-run is the default. This command never reads the private evidence map and
persists no URL, message GUID, or raw link metadata.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.contact_memory.imessage_link_review import (  # noqa: E402
    LINK_INTEREST_TAXONOMY, evidence_id, verify_review_id,
)
from gateway.contact_memory.schema import InterestEvent, InterestValence, SignalType  # noqa: E402
from gateway.contact_memory.store import ContactMemoryStore, normalize_interest_topic  # noqa: E402

_HEX_ID = re.compile(r"[0-9a-f]{64}")
_MONTH = re.compile(r"20\d{2}-(?:0[1-9]|1[0-2])")
_COUNT_KEY = re.compile(r"[a-z][a-z0-9_]*")
_SUBJECT_TARGET = {"kosta-owner": "poke", "stephen-lucier": "guest"}
_TOP_FIELDS = {"schema", "kind", "apply_supported", "candidates", "counts", "review_id"}
_CANDIDATE_FIELDS = {
    "candidate_id", "subject", "category", "topic", "distinct_evidence",
    "positive_reactions", "month_buckets", "evidence_ids",
}
_COUNT_FIELDS = {
    "link_occurrences", "distinct_links", "metadata_records", "candidate_topics", "excluded",
}


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _read_private(path: str | Path, *, label: str) -> bytes:
    source = Path(path).expanduser()
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"{label} must be a regular, non-symlink file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError(f"{label} must be mode 0600")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"{label} must be owned by the current user")
    return source.read_bytes()


def _integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _exact_fields(value: object, expected: set[str], *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    extras, missing = set(value) - expected, expected - set(value)
    if extras or missing:
        raise ValueError(f"{name} fields are invalid (extra={sorted(extras)}, missing={sorted(missing)})")
    return value


def _month_timestamp(month: str) -> float:
    if not _MONTH.fullmatch(month):
        raise ValueError("month bucket must use strict YYYY-MM format")
    # Exact message time was deliberately discarded. Use the UTC month boundary,
    # not apply time, as the deterministic timestamp for this aggregate evidence.
    return datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc).timestamp()


def validate_manifest(
    value: object, secret: bytes, approved_review_id: str | None = None,
) -> dict[str, list[InterestEvent]]:
    """Strictly validate a signed manifest and derive deterministic events."""
    manifest = _exact_fields(value, _TOP_FIELDS, name="manifest")
    if manifest["schema"] != 2 or manifest["kind"] != "imessage-link-interest-review":
        raise ValueError("unsupported link review schema or kind")
    if manifest["apply_supported"] is not False:
        raise ValueError("schema-2 extraction manifest must have apply_supported=false")
    review_id = manifest["review_id"]
    if not isinstance(review_id, str) or not _HEX_ID.fullmatch(review_id):
        raise ValueError("review_id must be a lowercase HMAC-SHA256 ID")
    if approved_review_id is not None and approved_review_id != review_id:
        raise ValueError("approved review ID does not exactly match manifest")
    if not verify_review_id(manifest, secret):
        raise ValueError("review_id HMAC verification failed")

    counts = _exact_fields(manifest["counts"], _COUNT_FIELDS, name="counts")
    for key in _COUNT_FIELDS - {"excluded"}:
        _integer(counts[key], name=f"counts.{key}")
    excluded = counts["excluded"]
    if not isinstance(excluded, Mapping) or any(
        not isinstance(key, str) or not _COUNT_KEY.fullmatch(key)
        or isinstance(item, bool) or not isinstance(item, int) or item < 0
        for key, item in excluded.items()
    ):
        raise ValueError("counts.excluded must contain non-negative integer counters")

    candidates = manifest["candidates"]
    if not isinstance(candidates, list):
        raise ValueError("candidates must be an array")
    if counts["candidate_topics"] != len(candidates):
        raise ValueError("candidate_topics count does not match candidates")
    batches: dict[str, list[InterestEvent]] = {subject: [] for subject in _SUBJECT_TARGET}
    seen_candidates: set[str] = set()
    seen_subject_topics: set[tuple[str, str]] = set()
    for index, raw in enumerate(candidates):
        candidate = _exact_fields(raw, _CANDIDATE_FIELDS, name=f"candidates[{index}]")
        subject, category, topic = candidate["subject"], candidate["category"], candidate["topic"]
        if subject not in _SUBJECT_TARGET:
            raise ValueError("unsupported candidate subject")
        if not isinstance(category, str) or not isinstance(topic, str) or (category, topic) not in LINK_INTEREST_TAXONOMY:
            raise ValueError("candidate category/topic is outside the link taxonomy")
        if normalize_interest_topic(topic) != topic:
            raise ValueError("candidate topic is not normalized")
        candidate_id = candidate["candidate_id"]
        expected_id = evidence_id(secret, "candidate", subject + "\0" + topic)
        if not isinstance(candidate_id, str) or not _HEX_ID.fullmatch(candidate_id) or candidate_id != expected_id:
            raise ValueError("candidate_id HMAC does not match subject/topic")
        if candidate_id in seen_candidates or (subject, topic) in seen_subject_topics:
            raise ValueError("duplicate candidate or subject/topic")
        seen_candidates.add(candidate_id)
        seen_subject_topics.add((subject, topic))

        distinct = _integer(candidate["distinct_evidence"], name="distinct_evidence")
        positive = _integer(candidate["positive_reactions"], name="positive_reactions")
        evidence_ids = candidate["evidence_ids"]
        if not isinstance(evidence_ids, list) or any(
            not isinstance(item, str) or not _HEX_ID.fullmatch(item) for item in evidence_ids
        ) or len(set(evidence_ids)) != len(evidence_ids) or evidence_ids != sorted(evidence_ids):
            raise ValueError("evidence_ids must be unique sorted HMAC-SHA256 IDs")
        if distinct != len(evidence_ids):
            raise ValueError("distinct_evidence does not match evidence_ids")
        if distinct < 2 and positive <= 0:
            raise ValueError("candidate has insufficient evidence")
        months = candidate["month_buckets"]
        if not isinstance(months, list) or not months or any(not isinstance(item, str) for item in months):
            raise ValueError("month_buckets must be a non-empty array")
        if months != sorted(set(months)):
            raise ValueError("month_buckets must be unique and sorted")
        created_at = _month_timestamp(months[-1])
        batches[subject].append(InterestEvent(
            event_id=candidate_id,
            topic_text=topic,
            signal_type=SignalType.ENGAGED_MENTION,
            valence=InterestValence.POSITIVE,
            source_id=f"reviewed_link_manifest:candidate:{candidate_id}",
            created_at=created_at,
        ))
    return batches


def _default_roots() -> dict[str, Path]:
    from hermes_cli.profiles import get_profile_dir
    return {subject: Path(get_profile_dir(target)) for subject, target in _SUBJECT_TARGET.items()}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-manifest", required=True)
    parser.add_argument("--hmac-key", help="0600 key (default: <manifest>.hmac-key)")
    parser.add_argument("--approved-review-id", help="exact operator-approved review HMAC; required with --apply")
    parser.add_argument("--poke-root", help="explicit Poke profile root")
    parser.add_argument("--guest-root", help="explicit Guest profile root")
    parser.add_argument("--apply", action="store_true", help="write atomically after explicit approval")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.apply and not args.approved_review_id:
        parser.error("--apply requires --approved-review-id")

    manifest_path = Path(args.review_manifest).expanduser()
    key_path = Path(args.hmac_key).expanduser() if args.hmac_key else manifest_path.with_suffix(manifest_path.suffix + ".hmac-key")
    try:
        manifest = json.loads(
            _read_private(manifest_path, label="review manifest"), object_pairs_hook=_strict_object,
        )
        secret = _read_private(key_path, label="HMAC key")
        if len(secret) < 16:
            raise ValueError("HMAC key must contain at least 16 bytes")
        batches = validate_manifest(manifest, secret, args.approved_review_id if args.apply else None)
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    summary: dict[str, Any] = {
        "dry_run": not args.apply,
        "review_id": manifest["review_id"],
        "subjects": {subject: len(events) for subject, events in batches.items()},
    }
    if args.apply:
        roots = {
            "kosta-owner": Path(args.poke_root).expanduser() if args.poke_root else None,
            "stephen-lucier": Path(args.guest_root).expanduser() if args.guest_root else None,
        }
        if any(root is None for root in roots.values()):
            defaults = _default_roots()
            roots = {subject: root or defaults[subject] for subject, root in roots.items()}
        results = {}
        for subject, events in batches.items():
            if not events:
                continue
            review_id = manifest["review_id"]
            store_manifest = {
                "schema": 1, "kind": "reviewed-link-interest-seed",
                "review_id": review_id, "candidate_ids": [event.event_id for event in events],
            }
            results[subject] = ContactMemoryStore(
                roots[subject] / "contact-memory", subject,  # type: ignore[operator]
            ).import_reviewed_interest_seed(
                events,
                run_id=f"reviewed-link-interest-seed:{review_id}",
                source_hash=review_id,
                manifest=store_manifest,
                now=max(event.created_at for event in events),
            )
        summary["results"] = results
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
