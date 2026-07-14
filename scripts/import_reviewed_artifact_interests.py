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

from gateway.contact_memory.schema import (  # noqa: E402
    InterestEvent, InterestValence, SignalType,
)
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

# Operator-reviewed projection from the merged dossier to concise proactive
# topics. Unlisted rows never become interests merely because they carry a
# broad ``preference`` label.
_CURATED_TOPICS: dict[str, tuple[str, str]] = {
    "kosta_pbj_obsession": ("kosta-owner", "PB&J spicy ajvar"),
    "kosta_edm_festivals": ("kosta-owner", "EDM festivals artists"),
    "kosta_cooking": ("kosta-owner", "cooking meal ideas"),
    "kosta_photography": ("kosta-owner", "photography birds drones"),
    "kosta_ai_tech": ("kosta-owner", "AI agent systems"),
    "kosta_weather_preference": ("kosta-owner", "cool overcast weather"),
    "kosta_nerdy_interests": ("kosta-owner", "space sci-fi PC building"),
    "kosta_fall_activities": ("kosta-owner", "fall foliage hiking"),
    "F8D061BE-CCB1-4571-9A2B-5B4D431D1EFF": ("stephen-lucier", "cuddling quality time"),
    "1464A261-B77B-48A2-A9A9-6A0ECAFCB485": ("stephen-lucier", "oversized gym clothes"),
    "146F06CE-71C9-4DF4-9918-CB0F8BDD6A22": ("stephen-lucier", "cooking tidy home"),
    "09139413-CD1D-49D2-A732-88D3219B1D9E": ("stephen-lucier", "Titanic movie nights"),
    "174ACB70-3BA6-4F40-A7B0-5B684756F13A": ("stephen-lucier", "donuts brownies sour candy"),
}
_CURATED_FACT_SHA256 = {
    "kosta_pbj_obsession": "ab016c056640d935956a07b38e3aa75ba8d5d7c4f072c7b8c4c693d330a902cd",
    "kosta_edm_festivals": "43e1d2812db69898961175b65c907698ab37d57b13842d5cff746ce8afcc603a",
    "kosta_cooking": "468c137e774d8466c50e366364772416e21a9b0760ab9aeeee11a721ecbbce44",
    "kosta_photography": "74aa8e8e67ef0f48e0dc8d74183f4e2d07719b8753c3e8a0aba2d4a7276cfcfc",
    "kosta_ai_tech": "3a8477ee82dd309227873a3f5fe6cdef606782bafeac602bff91bfe5020fc1fa",
    "kosta_weather_preference": "e4188a22416d0df74b1f3f492eaf642411eab136867d27330241f64c50a2fc5f",
    "kosta_nerdy_interests": "f56fe90574b0bc24d61d3b6294e82860d4a743dfbc0ec36c270f6e9fe77933a7",
    "kosta_fall_activities": "557b048ce3d48a7df782f4aac248f55d9b4f99154c177fd9343946fe3ae1d091",
    "F8D061BE-CCB1-4571-9A2B-5B4D431D1EFF": "9aadf3f56d12d35889b8f982dc06eecf1481f64642cfbb6b4133a1405beea33a",
    "1464A261-B77B-48A2-A9A9-6A0ECAFCB485": "3e22ac4b2488be9b0b3e5ae14535fc1503c9d2d0762d50617c5627eb310be672",
    "146F06CE-71C9-4DF4-9918-CB0F8BDD6A22": "a00203efd69c9d5b218c34af0a10eaff440a56d536527476d43313fe57d310d1",
    "09139413-CD1D-49D2-A732-88D3219B1D9E": "47835b1d21b203f3503490fae09ce5ebd1ea65b77f7237fd60ca731392dd1d88",
    "174ACB70-3BA6-4F40-A7B0-5B684756F13A": "6e7294e7c363560b7d8fc6676bf9807568091b41e13ad217cd9628f275dac17e",
}


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
    candidates: list[tuple[list[InterestEvent], dict[str, Any]]] = []
    for fact_line, fact, fact_raw in fact_rows:
        if str(fact.get("fact_type") or "").casefold() != "preference":
            excluded["not_explicit_preference"] += 1
            continue
        if str(fact.get("sensitivity") or "").casefold() != "normal" or fact.get("needs_review") is not False:
            excluded["artifact_sensitive_or_unreviewed"] += 1
            continue
        record_id = str(fact.get("source_record_id") or "").strip()
        mapping_key = str(fact.get("fact_id") or record_id)
        curated = _CURATED_TOPICS.get(mapping_key)
        if curated is None:
            excluded["not_curated_for_proactive"] += 1
            continue
        if _CURATED_FACT_SHA256.get(mapping_key) != _sha256(fact_raw):
            excluded["curated_fact_changed"] += 1
            continue
        subject, curated_topic = curated
        fact_text = str(fact.get("fact_value") or "").strip()
        leading_subject = (
            "stephen-lucier" if fact_text.casefold().startswith(("stephen ", "stephen's "))
            else "kosta-owner" if fact_text.casefold().startswith(("kosta ", "kosta's "))
            else None
        )
        if leading_subject is not None and leading_subject != subject:
            excluded["fact_subject_mismatch"] += 1
            continue
        matches = evidence_by_record.get(record_id, [])
        if matches:
            if len(matches) != 1:
                excluded["missing_or_ambiguous_evidence"] += 1
                continue
            evidence_line, evidence, evidence_raw = matches[0]
            speaker = str(evidence.get("speaker") or "").strip().casefold()
            if _SPEAKERS.get(speaker) != subject:
                excluded["fact_subject_mismatch"] += 1
                continue
        else:
            # Canonical kosta_* facts were already merged and reviewed with an
            # explicit subject. Legacy null-ID rows still require speaker evidence.
            if _subject_hint(fact) != subject:
                excluded["missing_or_ambiguous_evidence"] += 1
                continue
            evidence_line, evidence, evidence_raw = 0, {}, b""
        fact_text = str(fact.get("fact_value") or "")
        evidence_text = str(evidence.get("summary") or "")
        if _sensitive(fact_text, evidence_text):
            excluded["sensitive_topic"] += 1
            continue
        try:
            topic = _topic(curated_topic)
            created_at = _timestamp(evidence.get("timestamp") or fact.get("source_timestamp"))
        except (TypeError, ValueError, OverflowError):
            excluded["invalid_topic_or_timestamp"] += 1
            continue

        evidence_hash = _sha256(evidence_raw) if evidence_raw else ""
        fact_hash = _sha256(fact_raw)
        source_base = f"reviewed-artifact:{subject}:{record_id}:{evidence_hash}:{fact_hash}"
        event = InterestEvent(
            event_id=_sha256((source_base + "\0bootstrap_seed").encode()),
            topic_text=topic,
            signal_type=SignalType.ENGAGED_MENTION,
            valence=InterestValence.POSITIVE,
            source_id=f"{source_base}:bootstrap_seed",
            created_at=created_at,
        )
        events = [event]
        review = {
            "target": _TARGETS[subject],
            "contact_id": subject,
            "event_ids": [event.event_id for event in events],
            "topic": topic,
            "source_ids": [event.source_id for event in events],
            "evidence_pointer": {
                "source_record_id": record_id,
                "evidence_id": str(evidence.get("evidence_id") or ""),
                "line": evidence_line,
                "row_sha256": evidence_hash,
            },
            "fact_pointer": {"line": fact_line, "row_sha256": fact_hash},
            "created_at": created_at,
        }
        candidates.append((events, review))

    # One evidence record may back duplicate reviewed facts. Keep the shortest,
    # then lexical, topic so source evidence cannot be counted twice.
    candidates.sort(key=lambda item: (
        item[1]["contact_id"], item[1]["evidence_pointer"]["source_record_id"],
        len(item[0][0].topic_text), item[0][0].topic_text, item[0][0].source_id,
    ))
    deduped: list[tuple[list[InterestEvent], dict[str, Any]]] = []
    seen_evidence: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate[1]["contact_id"], candidate[1]["evidence_pointer"]["source_record_id"])
        if key in seen_evidence:
            excluded["duplicate_evidence"] += 1
            continue
        seen_evidence.add(key)
        deduped.append(candidate)

    batches = {subject: [] for subject in _TARGETS}
    ordered = sorted(deduped, key=lambda item: (item[1]["contact_id"], item[0][0].source_id))
    for events, review in ordered:
        batches[review["contact_id"]].extend(events)
    manifest = {
        "schema": _SCHEMA,
        "kind": "reviewed-artifact-interest-review",
        "inputs": {"facts_sha256": _sha256(fact_bytes), "evidence_sha256": _sha256(evidence_bytes)},
        "events": [review for _, review in ordered],
        "counts": {
            "facts_rows": len(fact_rows),
            "evidence_rows": len(evidence_rows),
            "accepted_topics": len(deduped),
            "accepted_events": sum(len(events) for events in batches.values()),
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
    # Preflight both isolated stores before either import starts. Per-target
    # import_run rows make an interrupted two-store apply explicit and safely
    # resumable; each store receives only its own filtered review payload.
    stores = {
        subject: ContactMemoryStore(Path(roots[subject]) / "contact-memory", subject)
        for subject in _TARGETS
    }
    results = {}

    for subject in _TARGETS:
        store = stores[subject]
        subject_events = [
            event for event in manifest["events"] if event.get("contact_id") == subject
        ]
        subject_manifest = {
            "schema": manifest["schema"],
            "kind": "reviewed-artifact-interest-review-target",
            "approved_global_sha256": review_sha256,
            "contact_id": subject,
            "events": subject_events,
            "counts": {"accepted_topics": len(subject_events)},
        }
        result = store.import_reviewed_interest_seed(
            batches[subject],
            run_id=f"reviewed-artifact-interests:{review_sha256}:{subject}",
            source_hash=f"{review_sha256}:{subject}",
            manifest=subject_manifest,
        )
        results[subject] = result
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
