from __future__ import annotations

import hashlib
import json
import plistlib
from pathlib import Path
import socket
import sqlite3
import urllib.error

import pytest

from gateway.contact_memory.imessage_bootstrap import open_messages_readonly, resolve_one_to_one_chat
from gateway.contact_memory.imessage_link_review import (
    FetchError, LinkSignal, build_review_manifest, canonicalize_url, classify_url,
    fetch_public_metadata, is_public_url, iter_link_signals,
)
from scripts.review_imessage_link_signals import main


def _db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE chat(ROWID INTEGER PRIMARY KEY,guid TEXT);
    CREATE TABLE handle(ROWID INTEGER PRIMARY KEY,id TEXT);
    CREATE TABLE chat_handle_join(chat_id INTEGER,handle_id INTEGER);
    CREATE TABLE message(
      ROWID INTEGER PRIMARY KEY,guid TEXT,date INTEGER,is_from_me INTEGER,text TEXT,
      attributedBody BLOB,associated_message_type INTEGER DEFAULT 0,
      associated_message_guid TEXT,thread_originator_guid TEXT
    );
    CREATE TABLE chat_message_join(chat_id INTEGER,message_id INTEGER);
    """)
    con.execute("INSERT INTO chat VALUES(1,'iMessage;-;private-steve-chat')")
    con.execute("INSERT INTO handle VALUES(1,'+14015550100')")
    con.execute("INSERT INTO chat_handle_join VALUES(1,1)")
    attributed = plistlib.dumps({"NSString": "private caption https://instagram.com/reel/two?utm_source=x"})
    dual = plistlib.dumps({"NSString": "https://reddit.com/r/cars/attributed"})
    rows = [
        (1, "g1", 1_000_000_000, 0, "private raw note https://instagram.com/reel/one?utm_source=secret#frag", None, 0, None, None),
        (2, "g2", 2_000_000_000, 0, None, attributed, 0, None, None),
        # Canonical duplicate must not inflate repetition.
        (3, "g3", 3_000_000_000, 0, "https://instagram.com/reel/one?utm_medium=again", None, 0, None, None),
        # Positive reaction and direct threaded reply from the recipient.
        (4, "t1", 4_000_000_000, 1, "Loved an unknown private phrase", None, 2000, "p:0:g1", None),
        (5, "r1", 5_000_000_000, 1, "another secret reply", None, 0, None, "g2"),
        # Owner direction remains owner, but one link cannot become a topic.
        (6, "g4", 6_000_000_000, 1, "https://youtu.be/owner-only", dual, 0, None, None),
        # Repeated but excluded by deterministic sensitive path token.
        (7, "g5", 7_000_000_000, 0, "https://instagram.com/p/politics-one", None, 0, None, None),
        (8, "g6", 8_000_000_000, 0, "https://instagram.com/p/politics-two", None, 0, None, None),
    ]
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO chat_message_join VALUES(1,?)", [(row[0],) for row in rows])
    con.commit()
    con.close()


def _signals(path: Path):
    with open_messages_readonly(path) as con:
        chat = resolve_one_to_one_chat(con, ["(401) 555-0100"])
        return chat, list(iter_link_signals(con, chat))


def test_direction_attributed_body_tapbacks_replies_dedupe_and_tracking(tmp_path: Path):
    path = tmp_path / "chat.db"
    _db(path)
    _, signals = _signals(path)
    assert len([item for item in signals if item.domain == "instagram.com"]) == 4
    by_guid = {item.message_guid: item for item in signals if item.domain != "reddit.com"}
    assert by_guid["g1"].sender == "stephen-lucier"
    assert by_guid["g4"].sender == "kosta-owner"
    assert any(item.message_guid == "g4" and item.domain == "reddit.com" for item in signals)
    assert by_guid["g1"].positive_tapback is True
    assert by_guid["g2"].recipient_reply is True
    assert by_guid["g2"].canonical_url == "https://instagram.com/reel/two"
    assert "g3" not in by_guid
    assert canonicalize_url("HTTPS://Example.COM/a?z=2&utm_source=x&a=1#private") == "https://example.com/a?a=1&z=2"


def test_manifest_requires_repetition_excludes_sensitive_and_leaks_no_raw_text(tmp_path: Path):
    path = tmp_path / "chat.db"
    _db(path)
    chat, signals = _signals(path)
    manifest = build_review_manifest(chat, signals)
    assert [(row["subject"], row["topic"]) for row in manifest["candidates"]] == [
        ("stephen-lucier", "Instagram reels and posts")
    ]
    candidate = manifest["candidates"][0]
    assert candidate["distinct_links"] == 2
    assert candidate["positive_tapbacks"] == 1
    assert candidate["recipient_replies"] == 1
    dumped = json.dumps(manifest)
    for private in ("private raw note", "private caption", "another secret reply", "https://"):
        assert private not in dumped
    assert classify_url("https://instagram.com/p/politics-now") == (None, None)


def test_private_url_rejection_and_fetch_failure_are_local():
    def private_resolver(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    assert not is_public_url("http://example.test/", resolver=private_resolver)
    with pytest.raises(FetchError, match="non-public"):
        fetch_public_metadata("http://example.test/", resolver=private_resolver)

    def public_resolver(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    class FailedOpener:
        def open(self, request, timeout):
            raise urllib.error.URLError("backend private detail")

    with pytest.raises(FetchError, match="robots unavailable") as exc:
        fetch_public_metadata("https://example.com/a", resolver=public_resolver, opener=FailedOpener())
    assert "backend private detail" not in str(exc.value)


def test_idempotent_private_cli_and_no_apply_surface(tmp_path: Path):
    path = tmp_path / "chat.db"
    review = tmp_path / "private" / "review.json"
    _db(path)
    args = ["--chat-db", str(path), "--handle", "+14015550100", "--review-manifest", str(review)]
    assert main(args) == 0
    first = review.read_bytes()
    assert main(args) == 0
    assert review.read_bytes() == first
    value = json.loads(first)
    assert value["apply_supported"] is False
    assert len(value["review_sha256"]) == 64
    assert review.stat().st_mode & 0o077 == 0
    assert hashlib.sha256(first).hexdigest() != ""  # canonical bytes are stable and hashable


def test_wal_snapshot_sees_committed_rows(tmp_path: Path):
    path = tmp_path / "chat.db"
    _db(path)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO message VALUES(9,'g9',9000000000,0,'https://reddit.com/r/cars/one',NULL,0,NULL,NULL)")
    writer.execute("INSERT INTO chat_message_join VALUES(1,9)")
    writer.commit()
    try:
        _, signals = _signals(path)
        assert any(item.message_guid == "g9" for item in signals)
    finally:
        writer.close()
