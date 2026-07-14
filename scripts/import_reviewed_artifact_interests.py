#!/usr/bin/env python3
"""Convert reviewed contact artifacts into speaker-scoped interest batches.

Dry-run is the default: it writes only a compact review manifest. Applying is
possible only when the caller supplies that exact manifest and its explicitly
approved SHA-256 digest.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.contact_memory.import_contacts import import_typed_batch  # noqa: E402
from gateway.contact_memory.schema import InterestEvent, InterestValence, SignalType  # noqa: E402
from gateway.contact_memory.store import ContactMemoryStore  # noqa: E402

_SCHEMA = 1
_TARGETS = {"kosta-owner": "poke", "stephen-lucier": "guest"}
_SPEAKERS = {
    "kosta": "kosta-owner",
    "stephen": "stephen-lucier",
    "contact/handle": "stephen-lucier",
}
_SENSITIVE_WORDS = frozenset({
    # Sensitive, health, sexual, and drug material is never a proactive topic.
    "adderall", "anadrol", "aneurysm", "cancer", "clen", "cocaine", "crime", "dbol",
    "disease", "drug", "drugs", "fisting", "ghb", "gin", "grindr", "health",
    "hospital", "infection", "ketamine", "mdma", "medical", "medicine",
    "mephedrone", "molly", "npp", "pancreatitis", "poppers", "prescription",
    "sex", "sexual", "steroid", "steroids", "stoned", "tequila", "testosterone", "therapy", "tren",
    "trenbolone", "viagra", "vodka", "warhead", "weed", "liquor",
})
_WORD_RE = re.compile(r"[a-z0-9]+", re.I)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_jsonl(path: Path, data: bytes) -> list[tuple[int, dict[str, Any], bytes]]:
    rows = []
    for number, raw in enumerate(data.splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: row is not an object")
        rows.append((number, value, raw))
    return rows


def _subject_hint(fact: dict[str, Any]) -> str | None:
    fact_id = str(fact.get("fact_id") or "").casefold()
    text = str(fact.get("fact_value") or "").lstrip().casefold()
    if fact_id.startswith("kosta_") or text.startswith(("kosta ", "kosta's ")):
        return "kosta-owner"
    if fact_id.startswith("stephen_") or text.startswith(("stephen ", "stephen's ")):
        return "stephen-lucier"
    return None


def _timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        raise ValueError("evidence timestamp is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _topic(value: object) -> str:
    text = " ".join(str(value or "").split())
    topic = _SENTENCE_RE.split(text, maxsplit=1)[0].strip()
    if not topic or len(topic) > 240:
        raise ValueError("preference topic must be between 1 and 240 characters")
    return topic


def _sensitive(*values: object) -> bool:
    words = {word.casefold() for value in values for word in _WORD_RE.findall(str(value or ""))}
    return bool(words & _SENSITIVE_WORDS)


def _opposite_person(subject: str, text: str) -> bool:
    words = {word.casefold() for word in _WORD_RE.findall(text)}
    if subject == "kosta-owner":
        return bool(words & {"stephen", "steve"})
    return "kosta" in words


def build_batches(
    facts_path: str | Path, evidence_path: str | Path,
) -> tuple[dict[str, list[InterestEvent]], dict[str, Any]]:
    """Build deterministic typed batches and a text-minimal review manifest."""
    facts_path, evidence_path = Path(facts_path), Path(evidence_path)
    fact_bytes, evidence_bytes = facts_path.read_bytes(), evidence_path.read_bytes()
    # Parse the exact bytes bound into the manifest, not a second read that
    # could race an artifact replacement.
    fact_rows = _read_jsonl(facts_path, fact_bytes)
    evidence_rows = _read_jsonl(evidence_path, evidence_bytes)

    evidence_by_record: dict[str, list[tuple[int, dict[str, Any], bytes]]] = {}
    for row in evidence_rows:
        record_id = str(row[1].get("source_record_id") or "").strip()
        if record_id:
            evidence_by_record.setdefault(record_id, []).append(row)

    excluded: Counter[str] = Counter()
    candidates: list[tuple[InterestEvent, dict[str, Any]]] = []
    for fact_line, fact, fact_raw in fact_rows:
        if str(fact.get("fact_type") or "").casefold() != "preference":
            excluded["not_explicit_preference"] += 1
            continue
        if str(fact.get("sensitivity") or "").casefold() != "normal" or fact.get("needs_review") is not False:
            excluded["artifact_sensitive_or_unreviewed"] += 1
            continue
        record_id = str(fact.get("source_record_id") or "").strip()
        matches = evidence_by_record.get(record_id, [])
        if len(matches) != 1:
            excluded["missing_or_ambiguous_evidence"] += 1
            continue
        evidence_line, evidence, evidence_raw = matches[0]
        speaker = str(evidence.get("speaker") or "").strip().casefold()
        subject = _SPEAKERS.get(speaker)
        if subject is None:
            excluded["ambiguous_speaker"] += 1
            continue
        if _subject_hint(fact) != subject:
            excluded["fact_subject_mismatch"] += 1
            continue
        fact_text = str(fact.get("fact_value") or "")
        evidence_text = str(evidence.get("summary") or "")
        if _sensitive(fact_text, evidence_text):
            excluded["sensitive_topic"] += 1
            continue
        if _opposite_person(subject, fact_text):
            excluded["third_party_material"] += 1
            continue
        try:
            topic = _topic(fact_text)
            created_at = _timestamp(evidence.get("timestamp"))
        except (TypeError, ValueError, OverflowError):
            excluded["invalid_topic_or_timestamp"] += 1
            continue

        evidence_hash = _sha256(evidence_raw)
        fact_hash = _sha256(fact_raw)
        source_id = f"reviewed-artifact:{subject}:{record_id}:{evidence_hash}:{fact_hash}"
        event = InterestEvent(
            event_id=_sha256((source_id + "\0interest-event").encode()),
            topic_text=topic,
            signal_type=SignalType.ENGAGED_MENTION,
            valence=InterestValence.POSITIVE,
            source_id=source_id,
            created_at=created_at,
        )
        review = {
            "target": _TARGETS[subject],
            "contact_id": subject,
            "event_id": event.event_id,
            "topic": topic,
            "source_id": source_id,
            "evidence_pointer": {
                "source_record_id": record_id,
                "evidence_id": str(evidence.get("evidence_id") or ""),
                "line": evidence_line,
                "row_sha256": evidence_hash,
            },
            "fact_pointer": {"line": fact_line, "row_sha256": fact_hash},
            "created_at": created_at,
        }
        candidates.append((event, review))

    # One evidence record may back duplicate reviewed facts. Keep the shortest,
    # then lexical, topic so source evidence cannot be counted twice.
    candidates.sort(key=lambda item: (
        item[1]["contact_id"], item[1]["evidence_pointer"]["source_record_id"],
        len(item[0].topic_text), item[0].topic_text, item[0].source_id,
    ))
    deduped: list[tuple[InterestEvent, dict[str, Any]]] = []
    seen_evidence: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate[1]["contact_id"], candidate[1]["evidence_pointer"]["source_record_id"])
        if key in seen_evidence:
            excluded["duplicate_evidence"] += 1
            continue
        seen_evidence.add(key)
        deduped.append(candidate)

    batches = {subject: [] for subject in _TARGETS}
    for event, review in sorted(deduped, key=lambda item: (item[1]["contact_id"], item[0].source_id)):
        batches[review["contact_id"]].append(event)
    manifest = {
        "schema": _SCHEMA,
        "kind": "reviewed-artifact-interest-review",
        "inputs": {"facts_sha256": _sha256(fact_bytes), "evidence_sha256": _sha256(evidence_bytes)},
        "events": [review for _, review in sorted(deduped, key=lambda item: (item[1]["contact_id"], item[0].source_id))],
        "counts": {
            "facts_rows": len(fact_rows),
            "evidence_rows": len(evidence_rows),
            "accepted": len(deduped),
            "by_target": {subject: len(batches[subject]) for subject in _TARGETS},
            "excluded": dict(sorted(excluded.items())),
        },
    }
    return batches, manifest


def _write_private(path: Path, data: bytes) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".interest-review.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def apply_batches(
    batches: dict[str, list[InterestEvent]], manifest: dict[str, Any],
    roots: dict[str, Path], *, review_sha256: str,
) -> dict[str, Any]:
    """Apply already integrity-checked batches to exactly two namespaces."""
    if set(roots) != set(_TARGETS) or set(batches) != set(_TARGETS):
        raise ValueError("exact Poke and Guest target namespaces are required")
    results = {}
    for subject in _TARGETS:
        store = ContactMemoryStore(Path(roots[subject]) / "contact-memory", subject)
        results[subject] = import_typed_batch(
            store=store,
            facts=[],
            interests=batches[subject],
            run_id=f"reviewed-artifact-interests:{review_sha256}:{subject}",
            source_hash=f"{review_sha256}:{subject}",
            manifest=manifest,
            dry_run=False,
        )
    return results


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", required=True, help="reviewed facts JSONL")
    parser.add_argument("--evidence", required=True, help="reviewed evidence JSONL")
    parser.add_argument("--review-manifest", required=True, help="review manifest to write (dry-run) or verify (apply)")
    parser.add_argument("--apply", action="store_true", help="apply only after explicit manifest-hash approval")
    parser.add_argument("--approved-review-sha256", help="explicitly approved SHA-256 of --review-manifest")
    args = parser.parse_args(list(argv) if argv is not None else None)

    batches, manifest = build_batches(args.facts, args.evidence)
    canonical = _canonical_bytes(manifest)
    digest = _sha256(canonical)
    review_path = Path(args.review_manifest).expanduser().resolve()
    if not args.apply:
        _write_private(review_path, canonical)
        print(json.dumps({
            "dry_run": True,
            "review_manifest": str(review_path),
            "review_sha256": digest,
            "counts": manifest["counts"],
            "apply_requires": "--apply --approved-review-sha256 <review_sha256>",
        }, indent=2, sort_keys=True))
        return 0

    if not args.approved_review_sha256:
        parser.error("--apply requires --approved-review-sha256")
    try:
        actual = review_path.read_bytes()
    except OSError as exc:
        parser.error(f"cannot read approved review manifest: {exc}")
    if _sha256(actual) != args.approved_review_sha256 or args.approved_review_sha256 != digest:
        parser.error("approved review manifest hash does not match current artifacts and converter output")
    if actual != canonical:
        parser.error("review manifest is not the canonical current converter output")

    from hermes_cli.profiles import get_profile_dir
    roots = {
        "kosta-owner": get_profile_dir("poke").expanduser().resolve(),
        "stephen-lucier": get_profile_dir("guest").expanduser().resolve(),
    }
    if roots["kosta-owner"] == roots["stephen-lucier"]:
        parser.error("Poke and Guest profile roots must be distinct")
    results = apply_batches(batches, manifest, roots, review_sha256=digest)
    print(json.dumps({"dry_run": False, "review_sha256": digest, "results": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
