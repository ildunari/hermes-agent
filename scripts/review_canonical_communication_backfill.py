#!/usr/bin/env python3
"""Prepare or explicitly apply a reviewed canonical iMessage backfill."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.contact_memory.imessage_bootstrap import (  # noqa: E402
    _normalize_handle,
    open_messages_readonly,
    resolve_one_to_one_chat,
)
from gateway.contact_memory.imessage_communication_adapter import (  # noqa: E402
    scan_historical_communication,
)
from gateway.contact_memory.reviewed_backfill import (  # noqa: E402
    ReviewedBackfill,
    apply_subject_backfill,
    build_candidate_selection,
    build_reviewed_backfill,
    read_existing_reviewed_state,
    restore_rehearsal,
    subject_review,
    verify_candidate_selection,
)
from gateway.contact_memory.store import ContactMemoryStore, opaque_contact_filename  # noqa: E402

_SUBJECT_ROOT = {"kosta-owner": "poke", "stephen-lucier": "guest"}


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _read_owner_only(path: Path, *, label: str) -> bytes:
    source = path.expanduser()
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"{label} must be a regular, non-symlink file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError(f"{label} must be mode 0600")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"{label} must be owned by the current user")
    return source.read_bytes()


def _write_owner_only(path: Path, data: bytes) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".phase-e-review.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _artifact_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    allowed = (Path.home() / ".hermes/profiles/coding/artifacts").resolve()
    if root != allowed and allowed not in root.parents:
        raise ValueError("Phase E artifacts must stay under the coding profile artifact area")
    if root.exists():
        raise ValueError("Phase E artifact root must not already exist")
    return root


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


def _profile_roots() -> dict[str, Path]:
    return {
        subject: Path.home() / ".hermes/profiles" / profile / "contact-memory"
        for subject, profile in _SUBJECT_ROOT.items()
    }


def _candidate_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": manifest["schema"],
        "kind": "reviewed-canonical-communication-candidate-summary",
        "global_review_id": manifest["global_review_id"],
        "subject_review_ids": {
            item["subject"]: item["subject_review_id"]
            for item in manifest["subject_reviews"]
        },
        "counts": manifest["counts"],
        "exclusions": manifest["exclusions"],
        "evaluation": manifest["evaluation"],
        "watchlist": manifest["watchlist"],
        "candidates": [
            {
                "candidate_id": item["candidate_id"],
                "subject": item["subject"],
                "kind": item["kind"],
                "label": item["label"],
                "entity_type": item["entity_type"],
                "polarity": item["polarity"],
                "eligibility": item["eligibility"],
                "occurrence_count": item["occurrence_count"],
                "distinct_days": item["distinct_days"],
            }
            for item in manifest["candidates"]
        ],
        "approval_boundary": manifest["approval_boundary"],
    }


def _source_accounting(scan: Any) -> dict[str, Any]:
    """Aggregate-only report: no text, URL, GUID, handle, or filesystem path."""
    return {
        "schema": 2,
        "kind": "phase-e-v3-aggregate-source-accounting",
        "accounting": dict(scan.accounting),
        "rejected_count": len(scan.rejected_evidence),
        "raw_source_retained": False,
    }


def _lock_down_tree(root: Path) -> None:
    """Make every generated review artifact owner-only, including directories."""
    for path in sorted(root.rglob("*"), reverse=True):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    os.chmod(root, 0o700)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-db", required=True)
    parser.add_argument("--handle", action="append", required=True)
    parser.add_argument("--chat-id", type=int)
    parser.add_argument(
        "--approved-handle-id", action="append", type=int, default=[],
        help="explicitly reviewed historical alias handle ID (repeatable)",
    )
    parser.add_argument("--hmac-key", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--metadata-cache", help="optional bounded owner-only metadata JSON")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--subject", choices=sorted(_SUBJECT_ROOT))
    parser.add_argument("--approved-subject-review-id")
    parser.add_argument(
        "--approved-candidate-id", action="append", default=[],
        help="explicitly approved candidate ID for the selected subject (repeatable)",
    )
    parser.add_argument("--review-manifest", help="exact signed manifest produced by dry-run")
    parser.add_argument("--approved-selection", help="owner-only signed candidate subset artifact")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.apply and (
        not args.subject or not args.approved_subject_review_id
        or (not args.approved_candidate_id and not args.approved_selection)
        or not args.review_manifest
    ):
        parser.error(
            "--apply requires --subject, --approved-subject-review-id, at least one "
            "--approved-candidate-id (or --approved-selection), and --review-manifest"
        )

    try:
        secret = _read_owner_only(Path(args.hmac_key), label="HMAC key")
        if len(secret) < 16:
            raise ValueError("HMAC key must contain at least 16 bytes")
        metadata = None
        if args.metadata_cache:
            metadata = json.loads(_read_owner_only(
                Path(args.metadata_cache), label="metadata cache",
            ))
        roots = _profile_roots()
        existing = {
            subject: read_existing_reviewed_state(roots[subject], subject)
            for subject in _SUBJECT_ROOT
        }
        with open_messages_readonly(args.chat_db) as con:
            con.execute("BEGIN")
            chat = resolve_one_to_one_chat(con, args.handle, chat_id=args.chat_id)
            if args.approved_handle_id:
                approved_aliases = {
                    _normalize_handle(value) for value in args.handle
                    if _normalize_handle(value)
                }
                requested = tuple(sorted(set(args.approved_handle_id)))
                rows = con.execute(
                    f"SELECT ROWID,id FROM handle WHERE ROWID IN ({','.join('?' for _ in requested)})",
                    requested,
                ).fetchall()
                if len(rows) != len(requested) or any(
                    _normalize_handle(row["id"]) not in approved_aliases for row in rows
                ):
                    raise ValueError(
                        "explicit historical handle IDs do not match approved aliases"
                    )
                chat = replace(chat, approved_handle_ids=requested)
            scan = scan_historical_communication(con, chat, secret=secret)
            con.rollback()
        review = build_reviewed_backfill(
            scan, secret=secret, existing=existing, metadata_cache=metadata,
        )

        if args.apply:
            approved_bytes = _read_owner_only(
                Path(args.review_manifest), label="review manifest",
            )
            approved_review = ReviewedBackfill(manifest=json.loads(approved_bytes))
            approved_subject = subject_review(approved_review, args.subject)
            if args.approved_subject_review_id != approved_subject["subject_review_id"]:
                raise ValueError("approved subject review ID does not exactly match the snapshot")
            approved_candidate_ids = list(args.approved_candidate_id)
            if args.approved_selection:
                selection = json.loads(_read_owner_only(
                    Path(args.approved_selection), label="candidate selection",
                ))
                if not verify_candidate_selection(selection, approved_review, secret=secret):
                    raise ValueError("candidate selection verification failed")
                if selection.get("subject") != args.subject:
                    raise ValueError("candidate selection crosses subject boundary")
                selected_ids = [item["candidate_id"] for item in selection["candidates"]]
                if approved_candidate_ids and sorted(approved_candidate_ids) != sorted(selected_ids):
                    raise ValueError("candidate IDs do not exactly match signed selection")
                approved_candidate_ids = selected_ids
            root = roots[args.subject]
            store_path = root / "contacts" / opaque_contact_filename(args.subject)
            if not store_path.is_file():
                raise FileNotFoundError("subject contact store does not exist")
            output = _artifact_root(Path(args.artifact_root))
            output.mkdir(parents=True, mode=0o700)
            backup = output / "pre-apply-backup.sqlite3"
            _sqlite_backup(store_path, backup)
            store = ContactMemoryStore(root, args.subject)
            result = apply_subject_backfill(
                scan, approved_review, subject=args.subject, store=store,
                approved_subject_review_id=args.approved_subject_review_id,
                approved_candidate_ids=approved_candidate_ids, secret=secret,
            )
            _write_owner_only(output / "apply-result.json", _canonical_bytes(result))
            print(json.dumps({
                "dry_run": False, "subject": args.subject,
                "subject_review_id": args.approved_subject_review_id,
                "backup": str(backup), "result": result,
            }, indent=2, sort_keys=True))
            return 0

        output = _artifact_root(Path(args.artifact_root))
        output.mkdir(parents=True, mode=0o700)
        aggregate = output / "aggregate-review-manifest.json"
        source_accounting = output / "source-accounting.json"
        summary = output / "candidate-summary.json"
        _write_owner_only(aggregate, _canonical_bytes(review.manifest))
        _write_owner_only(source_accounting, _canonical_bytes(_source_accounting(scan)))
        _write_owner_only(summary, _canonical_bytes(_candidate_summary(review.manifest)))
        signed_selections = {
            "schema": 2,
            "kind": "phase-e-v3-signed-candidate-subsets",
            "selections": [
                build_candidate_selection(
                    review, subject=subject,
                    candidate_ids=[item["candidate_id"] for item in subject_review(review, subject)["candidates"]],
                    secret=secret,
                )
                for subject in _SUBJECT_ROOT
                if subject_review(review, subject)["candidates"]
            ],
        }
        selections_path = output / "signed-candidate-subsets.json"
        _write_owner_only(selections_path, _canonical_bytes(signed_selections))

        disposable = output / "disposable-stores"
        apply_results: dict[str, Any] = {}
        for subject in _SUBJECT_ROOT:
            disposable_root = disposable / subject
            live_path = roots[subject] / "contacts" / opaque_contact_filename(subject)
            if live_path.is_file():
                _sqlite_backup(
                    live_path,
                    disposable_root / "contacts" / opaque_contact_filename(subject),
                )
            store = ContactMemoryStore(disposable_root, subject)
            target_review = subject_review(review, subject)
            apply_results[subject] = apply_subject_backfill(
                scan, review, subject=subject, store=store,
                approved_subject_review_id=target_review["subject_review_id"],
                approved_candidate_ids=[
                    item["candidate_id"] for item in target_review["candidates"]
                ], secret=secret,
            )
        restore_results: dict[str, Any] = {}
        for subject in _SUBJECT_ROOT:
            source_path = roots[subject] / "contacts" / opaque_contact_filename(subject)
            if source_path.is_file():
                restore_results[subject] = restore_rehearsal(
                    scan, review, subject=subject, source_store=source_path,
                    rehearsal_root=output / "restore-rehearsal" / subject,
                    secret=secret,
                )
        rehearsal = {
            "schema": 1,
            "kind": "reviewed-canonical-communication-rehearsal",
            "disposable_apply": apply_results,
            "restore": restore_results,
        }
        _write_owner_only(output / "rehearsal-evidence.json", _canonical_bytes(rehearsal))
        _lock_down_tree(output)
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    print(json.dumps({
        "dry_run": True,
        "global_review_id": review.manifest["global_review_id"],
        "subject_review_ids": {
            item["subject"]: item["subject_review_id"]
            for item in review.manifest["subject_reviews"]
        },
        "artifact_root": str(output),
        "aggregate_manifest": str(aggregate),
        "source_accounting": str(source_accounting),
        "candidate_summary": str(summary),
        "signed_candidate_subsets": str(selections_path),
        "accounting": review.manifest["accounting"],
        "counts": review.manifest["counts"],
        "exclusions": review.manifest["exclusions"],
        "evaluation": review.manifest["evaluation"],
        "apply_requires": (
            "--apply --subject <kosta-owner|stephen-lucier> "
            "--approved-subject-review-id <exact-subject-review-id> "
            "--approved-candidate-id <candidate-id> [repeat] "
            "--review-manifest <exact-manifest>"
        ),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
