"""Privacy-preserving JSONL contact importer.

Dry-run is the default and reports counts plus source IDs only. It never copies
source text into git or a review manifest unless the operator explicitly imports
into the profile-local database.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home

from .schema import AssertionType, Audience, FactProposal, FactStatus, MentionPolicy
from .store import ContactMemoryStore


def _score(value: Any, *, default: float = 0.5) -> float:
    """Normalize extractor confidence labels without silently trusting them."""
    if isinstance(value, str):
        label = value.strip().lower()
        if label in {"high", "verified"}:
            return 0.9
        if label in {"medium", "moderate"}:
            return 0.7
        if label in {"low", "uncertain"}:
            return 0.4
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _classify(row: dict[str, Any], contact_id: str) -> tuple[FactProposal, str]:
    # Support both the normalized importer schema and the existing contact-persona
    # extractor schema. fact_id is a stable fact identifier; source_record_id is
    # retained separately as the evidence pointer.
    source_id = str(row.get("source_id") or row.get("fact_id") or row.get("id") or "").strip()
    if not source_id:
        # Older contact-persona rows did not assign fact_id consistently. Build a
        # stable content-addressed ID rather than using line numbers, which change
        # when an extractor reorders or merges rows.
        identity = {
            key: row.get(key)
            for key in (
                "person_id", "fact_type", "fact_value", "source",
                "source_record_id", "source_timestamp",
            )
        }
        canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        source_id = "contact-persona:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    subject = str(row.get("subject_id") or row.get("subject") or "ambiguous").strip().lower()
    text = str(row.get("object_text") or row.get("fact_value") or row.get("fact") or row.get("text") or "").strip()
    if not text:
        raise ValueError(f"{source_id}: row has no fact text")
    sensitivity = str(row.get("sensitivity") or row.get("mention_policy") or "").strip().lower()
    sensitive = bool(row.get("sensitive")) or sensitivity in {"sensitive", "restricted"}
    third_party = subject in {"ambiguous", "third_party", "third-party", "unknown"}
    explicit_guest = str(row.get("audience") or "").lower() == "guest_ok" and bool(row.get("guest_reviewed", False))
    stated = str(row.get("assertion_type") or "stated").lower() == "stated"
    confidence = _score(row.get("confidence"), default=0.5)
    trust = _score(row.get("trust"), default=confidence)
    # Explicit owner review is the authority for Guest visibility. A reviewed
    # fact may be intimate, sensitive, or include a third party when it is about
    # the guest or their shared relationship; the review is what grants that
    # guest access. Unreviewed sensitive/ambiguous rows still fail closed.
    guest_ok = explicit_guest and stated and confidence >= 0.7 and trust >= 0.7
    quarantined = (sensitive or third_party) and not guest_ok
    status = FactStatus.QUARANTINED if quarantined else FactStatus.ACTIVE
    audience = Audience.GUEST_OK if guest_ok else Audience.OWNER_ONLY
    policy = MentionPolicy.MENTIONABLE if guest_ok else (MentionPolicy.SENSITIVE if sensitive else MentionPolicy.BACKGROUND)
    proposal = FactProposal(
        logical_id=str(row.get("logical_id") or row.get("fact_id") or f"import:{source_id}"),
        subject_id=str(row.get("subject_id") or row.get("subject") or "entity:ambiguous"),
        predicate=str(row.get("predicate") or row.get("fact_type") or "has_context"), object_text=text,
        audience=audience, mention_policy=policy,
        assertion_type=AssertionType.STATED if stated else AssertionType.INFERRED,
        source_id=source_id, source_contact_id=contact_id,
        evidence_pointer=str(
            row.get("evidence_pointer")
            or row.get("source_record_id")
            or row.get("source_timestamp")
            or row.get("evidence")
            or ""
        ),
        trust=trust, confidence=confidence, status=status,
        metadata={"imported": True},
    )
    reason = "guest_ok" if guest_ok else "quarantined" if quarantined else "owner_only"
    return proposal, reason


def import_jsonl(path: str | Path, contact_id: str, *, root: str | Path | None = None, dry_run: bool = True) -> dict[str, Any]:
    source = Path(path).expanduser()
    counts: Counter[str] = Counter()
    source_ids: list[str] = []
    proposals: list[FactProposal] = []
    errors: list[dict[str, str]] = []
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        counts["rows"] += 1
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row is not an object")
            proposal, reason = _classify(row, contact_id)
            proposals.append(proposal)
            source_ids.append(proposal.source_id)
            counts[reason] += 1
        except Exception as exc:
            counts["rejected"] += 1
            errors.append({"line": str(number), "error": str(exc)})
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("duplicate source IDs in import")
    if not dry_run:
        if errors:
            # Applying a dossier is all-or-nothing, including parse/validation.
            # A valid prefix must not land when a later row is malformed.
            counts["imported"] = 0
            counts["not_imported_due_to_errors"] = len(proposals)
        else:
            store = ContactMemoryStore(root or get_hermes_home() / "contact-memory", contact_id)
            outcome = store.import_proposals(proposals)
            counts["imported"] = outcome["inserted"]
            counts["skipped_existing"] = outcome["skipped"]
    return {"dry_run": dry_run, "counts": dict(counts), "source_ids": source_ids, "errors": errors}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run or import a contact memory JSONL dossier")
    parser.add_argument("path")
    parser.add_argument("--contact-id", required=True)
    parser.add_argument("--root")
    parser.add_argument("--apply", action="store_true", help="write to profile-local SQLite (default is dry-run)")
    parser.add_argument("--manifest", help="optional output path for count/source-ID review manifest")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = import_jsonl(args.path, args.contact_id, root=args.root, dry_run=not args.apply)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.manifest:
        Path(args.manifest).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if not result["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
