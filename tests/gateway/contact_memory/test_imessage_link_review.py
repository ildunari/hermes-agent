from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path
import sqlite3

import pytest

from gateway.contact_memory.imessage_bootstrap import open_messages_readonly, resolve_one_to_one_chat
from gateway.contact_memory.imessage_link_review import (
    FetchError, build_evidence_map, build_review_manifest, canonicalize_url, evidence_id,
    fetch_public_metadata, is_public_url, is_safe_fetch_candidate, iter_link_signals,
    select_enrichment_queue,
)
from scripts.review_imessage_link_signals import main

SECRET = b"0123456789abcdef0123456789abcdef"


def _db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE chat(ROWID INTEGER PRIMARY KEY,guid TEXT,style INTEGER,display_name TEXT,group_id TEXT);
    CREATE TABLE handle(ROWID INTEGER PRIMARY KEY,id TEXT);
    CREATE TABLE chat_handle_join(chat_id INTEGER,handle_id INTEGER);
    CREATE TABLE message(
      ROWID INTEGER PRIMARY KEY,guid TEXT,date INTEGER,is_from_me INTEGER,text TEXT,
      attributedBody BLOB,associated_message_type INTEGER DEFAULT 0,
      associated_message_guid TEXT,thread_originator_guid TEXT,handle_id INTEGER
    );
    CREATE TABLE chat_message_join(chat_id INTEGER,message_id INTEGER);
    """)
    con.execute("INSERT INTO chat VALUES(1,'iMessage;-;private-steve-chat',43,NULL,NULL)")
    con.executemany("INSERT INTO handle VALUES(?,?)", [(1, "+14015550100"), (2, "steve@example.test"), (9, "+19995550100")])
    con.execute("INSERT INTO chat_handle_join VALUES(1,1)")
    # The sibling URL is rich-preview metadata and must never be extracted.
    attributed = plistlib.dumps({"NSString": "https://youtube.com/watch?v=movie-two", "preview": "https://cdn.example/private.jpg"})
    rows = [
        (1, "g1", 1_000_000_000, 0, "https://instagram.com/reel/gym-one?utm_source=x", None, 0, None, None, 1),
        (2, "g2", 2_000_000_000, 0, "https://youtu.be/movie-one", attributed, 0, None, None, 1),
        (3, "g3", 3_000_000_000, 0, None, attributed, 0, None, None, 1),
        # Duplicate occurrence is retained for ranking/accounting but not distinct evidence.
        (4, "g4", 4_000_000_000, 0, "https://instagram.com/reel/gym-one?utm_medium=again", None, 0, None, None, 1),
        # Slash and colon tapback forms; removal cancels g3, final add keeps g1.
        (5, "t1", 5_000_000_000, 1, "", None, 2000, "p:0/g1", None, None),
        (6, "t2", 6_000_000_000, 1, "", None, 2000, "p:0/g3", None, None),
        (7, "t3", 7_000_000_000, 1, "", None, 3000, "bp:0/g3", None, None),
        (8, "t4", 8_000_000_000, 1, "", None, 2001, "p:0:g1", None, None),
        (9, "r1", 9_000_000_000, 1, "No, I hate this", None, 0, None, "p:0/g2", None),
        (10, "risk", 10_000_000_000, 0, "https://youtube.com/watch?v=x&token=abcdefghijklmnopqrstuvwxyz0123456789", None, 0, None, None, 1),
    ]
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO chat_message_join VALUES(1,?)", [(row[0],) for row in rows])
    con.commit(); con.close()


def _signals(path: Path):
    with open_messages_readonly(path) as con:
        chat = resolve_one_to_one_chat(con, ["(401) 555-0100", "steve@example.test"])
        return chat, list(iter_link_signals(con, chat))


def _metadata(signals):
    values = {}
    for signal in signals:
        eid = evidence_id(SECRET, "url", signal.identity_url)
        if "gym-one" in signal.identity_url:
            values[eid] = {"title": "Bodybuilding hypertrophy workout", "description": "gym muscle"}
        elif "movie-one" in signal.identity_url or "movie-two" in signal.identity_url:
            values[eid] = {"title": "Film trailer and TV series", "provider_name": "YouTube"}
    return {"schema": 1, "metadata": values}


def test_visible_text_handle_validation_actor_edges_and_tapback_removal(tmp_path: Path):
    path = tmp_path / "chat.db"; _db(path)
    _, signals = _signals(path)
    assert not any("cdn.example" in signal.identity_url for signal in signals)
    # Populated text wins over attributedBody; fallback is used only for g3.
    assert sum(signal.identity_url == "https://youtube.com/watch?v=movie-two" for signal in signals) == 1
    g1 = next(signal for signal in signals if signal.message_guid == "g1")
    g2 = next(signal for signal in signals if signal.message_guid == "g2")
    g3 = next(signal for signal in signals if signal.message_guid == "g3")
    assert g1.shared_by == "stephen-lucier" and g1.positive_reactors == ("kosta-owner",)
    assert g2.replied_by == ("kosta-owner",) and not g2.positive_reactors
    assert not g3.positive_reactors  # add followed by slash-form removal

    con = sqlite3.connect(path)
    con.execute("UPDATE message SET handle_id=9 WHERE guid='g2'"); con.commit(); con.close()
    with pytest.raises(ValueError, match="unapproved handle_id"):
        _signals(path)


def test_unattributed_incoming_service_rows_are_omitted(tmp_path: Path):
    path = tmp_path / "chat.db"
    _db(path)
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO message VALUES(99,'service',9000000000,0,?,NULL,0,NULL,NULL,0)",
        ("https://youtube.com/watch?v=must-not-attribute",),
    )
    con.execute("INSERT INTO chat_message_join VALUES(1,99)")
    con.commit()
    con.close()
    _, signals = _signals(path)
    assert not any("must-not-attribute" in signal.identity_url for signal in signals)


def test_conservative_url_identity_and_risky_fetch_rejection():
    raw = "HTTPS://Example.COM/a%2Fb?z=2&utm_source=x&a=%2F&b=1#frag"
    assert canonicalize_url(raw) == "https://example.com/a%2Fb?z=2&a=%2F&b=1"
    assert canonicalize_url("https://e.test/a/b?b=1&a=2") != canonicalize_url("https://e.test/a/b?a=2&b=1")
    assert not is_safe_fetch_candidate("https://example.com/unsubscribe?id=12")
    assert not is_safe_fetch_candidate("https://youtube.com/watch?v=x&signature=abc")
    assert not is_safe_fetch_candidate("https://youtube.com/watch?v=x&foo=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def test_manifest_uses_metadata_for_actual_topics_and_is_aggregate_only(tmp_path: Path):
    path = tmp_path / "chat.db"; _db(path)
    chat, signals = _signals(path)
    manifest = build_review_manifest(chat, signals, secret=SECRET, metadata_cache=_metadata(signals))
    topics = {(item["subject"], item["topic"]) for item in manifest["candidates"]}
    # A positive tapback is evidence for the reactor. The one-time sharer does
    # not inherit an interest without separate repeated evidence.
    assert ("kosta-owner", "bodybuilding and strength training") in topics
    assert ("stephen-lucier", "bodybuilding and strength training") not in topics
    # Two distinct movie links establish a repeated content topic; the negative reply adds no valence.
    assert ("stephen-lucier", "film and television") in topics
    assert ("kosta-owner", "film and television") not in topics
    dumped = json.dumps(manifest)
    for forbidden in ("g1", "g2", "private-steve-chat", "instagram.com", "youtube.com", "https://", "created_at", "message_guid", "url_sha256", "chat_guid"):
        assert forbidden not in dumped
    assert all(set(item) == {"candidate_id", "subject", "category", "topic", "distinct_evidence", "positive_reactions", "month_buckets", "evidence_ids"} for item in manifest["candidates"])


def test_evidence_map_and_bounded_queue_are_separate_and_private_content(tmp_path: Path):
    path = tmp_path / "chat.db"; _db(path)
    chat, signals = _signals(path)
    evidence = build_evidence_map(chat, signals, SECRET)
    queue = select_enrichment_queue(signals, SECRET, max_requests=2)
    assert len(queue["requests"]) == 2
    assert all("url" in request for request in queue["requests"])
    assert not any("token=" in request["url"] for request in queue["requests"])
    assert "message_guid" in evidence["signals"][0]
    with pytest.raises(ValueError, match="between 0 and 50"):
        select_enrichment_queue(signals, SECRET, max_requests=51)


def test_direct_fetch_fails_closed_without_dns_rebinding_gap():
    assert is_public_url("https://example.com") is False
    with pytest.raises(FetchError, match="pinned transport"):
        fetch_public_metadata("https://example.com")


def test_direct_chat_alias_disambiguation_and_group_rejection(tmp_path: Path):
    path = tmp_path / "chat.db"; _db(path)
    con = sqlite3.connect(path); con.row_factory = sqlite3.Row
    con.execute("INSERT INTO chat VALUES(2,'iMessage;-;alias-chat',43,NULL,NULL)")
    con.execute("INSERT INTO chat_handle_join VALUES(2,2)")
    con.execute("INSERT INTO chat VALUES(3,'iMessage;+;stale-group',45,NULL,'group-id')")
    con.execute("INSERT INTO chat_handle_join VALUES(3,2)"); con.commit()
    with pytest.raises(ValueError, match="use chat_id"):
        resolve_one_to_one_chat(con, ["+14015550100", "steve@example.test"])
    resolved = resolve_one_to_one_chat(con, ["+14015550100", "steve@example.test"], chat_id=2)
    assert resolved.chat_id == 2 and set(resolved.approved_handle_ids) == {1, 2}
    with pytest.raises(ValueError, match="exactly one"):
        resolve_one_to_one_chat(con, ["steve@example.test"], chat_id=3)
    con.close()


def test_cli_e2e_offline_metadata_produces_topics_and_0600_artifacts(tmp_path: Path):
    path = tmp_path / "chat.db"; _db(path)
    review, evidence, queue = tmp_path / "review.json", tmp_path / "evidence.json", tmp_path / "queue.json"
    key = tmp_path / "key"; key.write_bytes(SECRET); os.chmod(key, 0o600)
    _, signals = _signals(path)
    cache = tmp_path / "metadata.json"; cache.write_text(json.dumps(_metadata(signals))); os.chmod(cache, 0o600)
    args = ["--chat-db", str(path), "--handle", "+14015550100", "--review-manifest", str(review),
            "--evidence-map", str(evidence), "--fetch-queue", str(queue), "--hmac-key", str(key),
            "--metadata-cache", str(cache), "--max-requests", "3"]
    assert main(args) == 0
    first = review.read_bytes(); assert main(args) == 0 and review.read_bytes() == first
    value = json.loads(first)
    assert "bodybuilding and strength training" in {item["topic"] for item in value["candidates"]}
    assert value["apply_supported"] is False
    for artifact in (review, evidence, queue, key, cache):
        assert artifact.stat().st_mode & 0o077 == 0
