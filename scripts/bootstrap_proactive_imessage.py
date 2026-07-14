#!/usr/bin/env python3
"""Build and optionally apply the two sender-attributed proactive dossiers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.contact_memory.imessage_bootstrap import (  # noqa: E402
    build_manifest, chunk_rows, iter_chat_rows, open_messages_readonly,
    resolve_one_to_one_chat, validate_semantic_items,
)
from gateway.contact_memory.import_contacts import _classify, import_typed_batch  # noqa: E402
from gateway.contact_memory.schema import InterestEvent, InterestValence, SignalType  # noqa: E402
from gateway.contact_memory.store import ContactMemoryStore  # noqa: E402

_ALLOWED_TARGETS = {"poke": "kosta-owner", "guest": "stephen-lucier"}


def _atomic_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".bootstrap.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except OSError: pass
        raise


def _load_reviewed(path: Path, manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("rowset_sha256") != manifest["rowset_sha256"]:
        raise ValueError("review manifest does not match the scanned source rowset")
    if value.get("provider") != "openai-codex" or value.get("model") != "gpt-5.6-sol" or value.get("reasoning_effort") != "medium":
        raise ValueError("semantic review must be openai-codex/gpt-5.6-sol/medium")
    if value.get("approved") is not True:
        raise ValueError("review manifest is not approved")
    dossiers = value.get("dossiers")
    if not isinstance(dossiers, dict):
        raise ValueError("review manifest has no dossiers")
    return {
        subject: validate_semantic_items(dossiers.get(subject), subject=subject)
        for subject in ("kosta-owner", "stephen-lucier")
    }


def _typed(items: list[dict[str, Any]], contact_id: str):
    facts, events = [], []
    for item in items:
        if item.get("suppressed"):
            continue
        source_id = str(item["source_id"])
        if item["kind"] == "fact":
            row = {
                "source_id": source_id, "logical_id": "bootstrap:" + hashlib.sha256(source_id.encode()).hexdigest(),
                "subject_id": contact_id, "predicate": item.get("predicate") or "has_context",
                "object_text": item["text"], "confidence": item["confidence"], "trust": item["confidence"],
                "sensitive": bool(item.get("sensitive")), "audience": item.get("audience") or "owner_only",
                "guest_reviewed": bool(item.get("guest_reviewed")), "assertion_type": "stated",
                "evidence_pointer": "sha256:" + hashlib.sha256(str(item["guid"]).encode()).hexdigest(),
            }
            facts.append(_classify(row, contact_id)[0])
        else:
            signal = SignalType(str(item.get("signal_type") or "engaged_mention"))
            valence = InterestValence(str(item.get("valence") or "positive"))
            events.append(InterestEvent(
                event_id=hashlib.sha256((source_id + "\0event").encode()).hexdigest(),
                topic_text=str(item["topic"]), signal_type=signal, valence=valence,
                source_id=source_id, created_at=float(item.get("created_at") or 0),
            ))
    return facts, events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-person", required=True)
    parser.add_argument("--chat-db", default="~/Library/Messages/chat.db")
    parser.add_argument("--handle", action="append", required=True, help="explicit approved Stephen handle; repeatable")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--staging-dir", default="~/.config/hermes-state/proactive-bootstrap")
    parser.add_argument("--poke-root", default="~/.hermes/profiles/poke")
    parser.add_argument("--guest-root", default="~/.hermes/profiles/guest")
    parser.add_argument("--poke-contact-id", default="kosta-owner")
    parser.add_argument("--guest-contact-id", default="stephen-lucier")
    parser.add_argument("--task", default="proactive_semantic")
    parser.add_argument("--prompt-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--review-manifest")
    args = parser.parse_args(argv)
    if args.source_person != "Stephen Lucier":
        parser.error("this rollout accepts only --source-person 'Stephen Lucier'")
    if args.task != "proactive_semantic":
        parser.error("semantic task must be proactive_semantic")
    if args.poke_contact_id != _ALLOWED_TARGETS["poke"] or args.guest_contact_id != _ALLOWED_TARGETS["guest"]:
        parser.error("canonical contact IDs are fixed for this rollout")
    if args.apply and not args.review_manifest:
        parser.error("--apply requires --review-manifest")
    if not args.apply:
        args.dry_run = True

    db_path = Path(args.chat_db).expanduser().resolve()
    with open_messages_readonly(db_path) as con:
        chat = resolve_one_to_one_chat(con, args.handle)
        chunks = list(chunk_rows(iter_chat_rows(con, chat, limit=args.limit), chunk_size=args.chunk_size))
    manifest = build_manifest(chat, chunks, source_path=db_path)
    manifest.update({"task": args.task, "provider": "openai-codex", "model": "gpt-5.6-sol",
                     "reasoning_effort": "medium", "limit": args.limit})
    staging = Path(args.staging_dir).expanduser().resolve()
    manifest_path = staging / f"manifest-{manifest['rowset_sha256']}.json"
    _atomic_private_json(manifest_path, manifest)

    if args.prompt_only:
        print(json.dumps({"manifest": str(manifest_path), **manifest}, indent=2, sort_keys=True))
        return 0
    if not args.review_manifest:
        print(json.dumps({"dry_run": True, "manifest": str(manifest_path),
                          "next": "run the pinned Sol-medium extraction/merge/review and pass --review-manifest",
                          "counts": {k: manifest[k] for k in ("selected", "represented", "explicit_non_text", "directions")}},
                         indent=2, sort_keys=True))
        return 0

    dossiers = _load_reviewed(Path(args.review_manifest).expanduser(), manifest)
    results = {}
    roots = {"kosta-owner": Path(args.poke_root).expanduser().resolve(),
             "stephen-lucier": Path(args.guest_root).expanduser().resolve()}
    run_id = "imessage-bootstrap:" + manifest["rowset_sha256"]
    for subject, items in dossiers.items():
        facts, interests = _typed(items, subject)
        store = ContactMemoryStore(roots[subject] / "contact-memory", subject)
        results[subject] = import_typed_batch(
            store=store, facts=facts, interests=interests, run_id=run_id + ":" + subject,
            source_hash=manifest["rowset_sha256"] + ":" + subject, manifest=manifest,
            dry_run=not args.apply,
        )
    print(json.dumps({"manifest": str(manifest_path), "results": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
