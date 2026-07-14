#!/usr/bin/env python3
"""Build a private, hash-bound iMessage link-interest review (never apply)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.contact_memory.imessage_bootstrap import (  # noqa: E402
    open_messages_readonly, resolve_one_to_one_chat,
)
from gateway.contact_memory.imessage_link_review import (  # noqa: E402
    FetchError, build_review_manifest, fetch_public_metadata, iter_link_signals,
)


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_private(path: Path, data: bytes) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".link-review.", dir=path.parent)
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


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-db", default="~/Library/Messages/chat.db")
    parser.add_argument("--handle", action="append", required=True,
                        help="explicitly approved 1:1 handle (repeatable)")
    parser.add_argument("--review-manifest", required=True)
    parser.add_argument("--enrich", action="store_true",
                        help="explicitly allow bounded public-web title enrichment")
    parser.add_argument("--enrichment-cache",
                        help="private local cache; required with --enrich")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.enrich and not args.enrichment_cache:
        parser.error("--enrich requires --enrichment-cache")

    with open_messages_readonly(args.chat_db) as con:
        con.execute("BEGIN")  # one WAL-aware read snapshot for resolution + extraction
        chat = resolve_one_to_one_chat(con, args.handle)
        signals = list(iter_link_signals(con, chat))
        con.rollback()

    enrichment = {}
    errors = {}
    if args.enrich:
        for signal in signals:
            try:
                enrichment[signal.url_sha256] = fetch_public_metadata(signal.canonical_url)
            except FetchError as exc:
                # Errors remain in the private cache, not the review manifest.
                errors[signal.url_sha256] = str(exc)
        _write_private(Path(args.enrichment_cache), _canonical_bytes({
            "schema": 1, "metadata": enrichment, "errors": errors,
        }))

    manifest = build_review_manifest(chat, signals)
    _write_private(Path(args.review_manifest), _canonical_bytes(manifest))
    print(json.dumps({
        "dry_run": True, "apply_supported": False,
        "review_manifest": str(Path(args.review_manifest).expanduser().resolve()),
        "review_sha256": manifest["review_sha256"], "counts": manifest["counts"],
        "enriched": len(enrichment), "enrichment_errors": len(errors),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())