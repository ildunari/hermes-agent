#!/usr/bin/env python3
"""Build reviewed historical communication artifacts in isolated temporary stores."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.contact_memory.imessage_bootstrap import (  # noqa: E402
    open_messages_readonly,
    resolve_one_to_one_chat,
)
from gateway.contact_memory.imessage_communication_adapter import (  # noqa: E402
    HistoricalCommunicationScan,
    HistoricalStoreTargets,
    build_aggregate_manifest,
    build_private_evidence_manifest,
    ingest_historical_scan,
    scan_historical_communication,
)
from gateway.contact_memory.store import ContactMemoryStore  # noqa: E402


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _write_private(path: Path, data: bytes) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".communication-review.", dir=destination.parent)
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


def _read_private_key(path: Path) -> bytes:
    source = path.expanduser()
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError("HMAC key must be a regular, non-symlink file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("HMAC key must be mode 0600")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError("HMAC key must be owned by the current user")
    secret = source.read_bytes()
    if len(secret) < 16:
        raise ValueError("HMAC key must contain at least 16 bytes")
    return secret


def _temporary_store_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    live_roots = {(Path.home() / ".hermes").resolve()}
    configured_home = os.environ.get("HERMES_HOME")
    if configured_home:
        live_roots.add(Path(configured_home).expanduser().resolve())
    if any(root == live or live in root.parents for live in live_roots):
        raise ValueError("temporary store root cannot be inside a live Hermes profile tree")
    return root


def _publish_review(
    *,
    store_root: Path,
    aggregate_path: Path,
    private_path: Path,
    scan: HistoricalCommunicationScan,
    secret: bytes,
) -> tuple[dict[str, dict[str, int]], dict[str, object]]:
    """Stage every output, then publish all-or-clean-none on any failure."""
    destinations = (store_root, aggregate_path, private_path)
    if len({path.resolve() for path in destinations}) != len(destinations):
        raise ValueError("review output destinations must be distinct")
    if any(
        store_root == artifact or store_root in artifact.parents
        for artifact in (aggregate_path, private_path)
    ):
        raise ValueError("review artifacts cannot be inside the temporary store root")
    if any(path.exists() for path in destinations):
        raise ValueError("review output destinations must not already exist")
    store_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(
        prefix=".communication-review-stage.", dir=store_root.parent
    ))
    stage.chmod(0o700)
    stage_stores = stage / "stores"
    stage_private = stage / "private.json"
    stage_aggregate = stage / "aggregate.json"
    published: list[Path] = []
    try:
        targets = HistoricalStoreTargets(
            owner=ContactMemoryStore(stage_stores / "poke", "kosta-owner"),
            guest=ContactMemoryStore(stage_stores / "guest", "stephen-lucier"),
        )
        ingest = ingest_historical_scan(scan, targets)
        aggregate = build_aggregate_manifest(scan, secret=secret)
        private = build_private_evidence_manifest(scan)
        _write_private(stage_private, _canonical_bytes(private))
        _write_private(stage_aggregate, _canonical_bytes(aggregate))

        for destination in (private_path, aggregate_path):
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(stage_stores, store_root)
        published.append(store_root)
        os.replace(stage_private, private_path)
        published.append(private_path)
        os.replace(stage_aggregate, aggregate_path)
        published.append(aggregate_path)
        return ingest, aggregate
    except BaseException:
        for destination in reversed(published):
            if destination.is_dir():
                shutil.rmtree(destination, ignore_errors=True)
            else:
                destination.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-db", required=True)
    parser.add_argument(
        "--handle", action="append", required=True,
        help="explicitly approved direct-chat alias (repeatable)",
    )
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--aggregate-manifest", required=True)
    parser.add_argument("--private-evidence", required=True)
    parser.add_argument("--hmac-key", required=True)
    parser.add_argument(
        "--temporary-store-root", required=True,
        help="isolated non-profile root for disposable Poke/Guest contact stores",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        secret = _read_private_key(Path(args.hmac_key))
        store_root = _temporary_store_root(Path(args.temporary_store_root))
        with open_messages_readonly(args.chat_db) as con:
            con.execute("BEGIN")
            chat = resolve_one_to_one_chat(con, args.handle, chat_id=args.chat_id)
            scan = scan_historical_communication(con, chat, secret=secret)
            con.rollback()
        ingest, aggregate = _publish_review(
            store_root=store_root,
            aggregate_path=Path(args.aggregate_manifest).expanduser().resolve(),
            private_path=Path(args.private_evidence).expanduser().resolve(),
            scan=scan,
            secret=secret,
        )
    except (OSError, ValueError, PermissionError) as exc:
        del exc
        parser.error("historical communication review failed")

    print(json.dumps({
        "dry_run": True,
        "apply_supported": False,
        "review_id": aggregate["review_id"],
        "accounting": aggregate["accounting"],
        "secondary_counts": aggregate["secondary_counts"],
        "temporary_store_ingest": ingest,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
