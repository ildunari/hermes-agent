#!/usr/bin/env python3
"""Build an offline, aggregate-only iMessage content-interest review (never apply)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.contact_memory.imessage_bootstrap import open_messages_readonly, resolve_one_to_one_chat  # noqa: E402
from gateway.contact_memory.imessage_link_review import (  # noqa: E402
    build_evidence_map, build_review_manifest, iter_link_signals, select_enrichment_queue,
)


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_private(path: Path, data: bytes) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".link-review.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _load_or_create_secret(path: Path) -> bytes:
    path = path.expanduser().resolve()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = secrets.token_bytes(32)
        _write_private(path, data)
    if len(data) < 16:
        raise ValueError("HMAC key must contain at least 16 bytes")
    if path.stat().st_mode & 0o077:
        raise PermissionError("HMAC key must be mode 0600")
    return data


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-db", default="~/Library/Messages/chat.db")
    parser.add_argument("--handle", action="append", required=True, help="approved direct-chat alias (repeatable)")
    parser.add_argument("--chat-id", type=int, help="exact direct chat ROWID; required when aliases match multiple chats")
    parser.add_argument("--review-manifest", required=True)
    parser.add_argument("--evidence-map", required=True, help="0600 drill-down map containing exact local evidence")
    parser.add_argument("--hmac-key", help="0600 local key (default: <manifest>.hmac-key)")
    parser.add_argument("--fetch-queue", help="optional 0600 bounded URL queue for a trusted pinned web tool")
    parser.add_argument("--max-requests", type=int, default=50, choices=range(0, 51))
    parser.add_argument("--metadata-cache", help="optional 0600 offline metadata JSON returned by the trusted tool")
    args = parser.parse_args(list(argv) if argv is not None else None)

    manifest_path = Path(args.review_manifest).expanduser().resolve()
    key_path = Path(args.hmac_key).expanduser().resolve() if args.hmac_key else manifest_path.with_suffix(manifest_path.suffix + ".hmac-key")
    secret = _load_or_create_secret(key_path)
    metadata: object = {}
    if args.metadata_cache:
        cache_path = Path(args.metadata_cache).expanduser().resolve()
        if cache_path.stat().st_mode & 0o077:
            raise PermissionError("metadata cache must be mode 0600")
        metadata = json.loads(cache_path.read_text(encoding="utf-8"))

    with open_messages_readonly(args.chat_db) as con:
        con.execute("BEGIN")
        chat = resolve_one_to_one_chat(con, args.handle, chat_id=args.chat_id)
        signals = list(iter_link_signals(con, chat))
        con.rollback()

    evidence = build_evidence_map(chat, signals, secret)
    manifest = build_review_manifest(chat, signals, secret=secret, metadata_cache=metadata)
    _write_private(Path(args.evidence_map), _canonical_bytes(evidence))
    queued = 0
    if args.fetch_queue:
        queue = select_enrichment_queue(signals, secret, max_requests=args.max_requests)
        queued = len(queue["requests"])
        _write_private(Path(args.fetch_queue), _canonical_bytes(queue))
    _write_private(manifest_path, _canonical_bytes(manifest))
    print(json.dumps({"dry_run": True, "apply_supported": False,
        "review_manifest": str(manifest_path), "review_id": manifest["review_id"],
        "counts": manifest["counts"], "queued": queued},
        indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
