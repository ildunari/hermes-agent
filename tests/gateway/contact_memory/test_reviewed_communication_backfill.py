from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from gateway.contact_memory.imessage_communication_adapter import (
    HistoricalStoreTargets,
    scan_historical_communication,
)
from gateway.contact_memory.imessage_bootstrap import open_messages_readonly, resolve_one_to_one_chat
from gateway.contact_memory.reviewed_backfill import (
    ExistingReviewedState,
    apply_subject_backfill,
    build_reviewed_backfill,
    restore_rehearsal,
    verify_review_manifest,
)
from gateway.contact_memory.store import ContactMemoryStore
from scripts.review_canonical_communication_backfill import main as cli_main

SECRET = b"phase-e-review-key-00000000000001"
DAY = 86_400_000_000_000


def _messages(path: Path) -> None:
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
    CREATE TABLE attachment(ROWID INTEGER PRIMARY KEY,guid TEXT,filename TEXT,mime_type TEXT,uti TEXT,total_bytes INTEGER);
    CREATE TABLE message_attachment_join(message_id INTEGER,attachment_id INTEGER);
    """)
    con.execute("INSERT INTO chat VALUES(1,'iMessage;-;direct',45,NULL,'internal')")
    con.execute("INSERT INTO handle VALUES(1,'steve@example.test')")
    con.execute("INSERT INTO chat_handle_join VALUES(1,1)")
    rows = [
        (1, "k-music-1", 1 * DAY, 1, "techno festival lineup", None, 0, None, None, None),
        (2, "k-music-2", 3 * DAY, 1, "another techno concert", None, 0, None, None, None),
        (3, "s-fitness-1", 2 * DAY, 0, "gym lifting workout", None, 0, None, None, 1),
        (4, "s-fitness-2", 4 * DAY, 0, "bodybuilding workout", None, 0, None, None, 1),
        (5, "s-youtube-1", 5 * DAY, 0, "https://youtube.com/watch?v=one", None, 0, None, None, 1),
        (6, "s-youtube-2", 6 * DAY, 0, "https://youtu.be/two", None, 0, None, None, 1),
        (7, "service", 7 * DAY, 0, "anonymous", None, 0, None, None, 0),
    ]
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO chat_message_join VALUES(1,?)", [(row[0],) for row in rows])
    con.commit()
    con.close()


def _scan(path: Path):
    with open_messages_readonly(path) as con:
        chat = resolve_one_to_one_chat(con, ["steve@example.test"])
        return scan_historical_communication(con, chat, secret=SECRET)


def test_review_manifest_is_deterministic_subject_correct_signed_and_aggregate_only(tmp_path: Path) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    scan = _scan(source)
    existing = {
        "kosta-owner": ExistingReviewedState(topics=frozenset({"electronic dance music"})),
        "stephen-lucier": ExistingReviewedState(),
    }

    first = build_reviewed_backfill(scan, secret=SECRET, existing=existing)
    second = build_reviewed_backfill(scan, secret=SECRET, existing=existing)

    assert first.manifest == second.manifest
    assert first.projections == second.projections
    assert verify_review_manifest(first.manifest, secret=SECRET)
    assert first.manifest["counts"]["by_subject"]["kosta-owner"]["topics"] == 0
    assert first.manifest["counts"]["by_subject"]["stephen-lucier"] == {
        "callbacks": 0, "entities": 1, "recommendations": 0, "topics": 1,
    }
    assert all(
        candidate["subject"] == "stephen-lucier"
        for candidate in first.manifest["candidates"]
    )
    assert first.manifest["exclusions"]["existing_reviewed_topic"] == 1
    aggregate = json.dumps(first.manifest, sort_keys=True)
    for raw in ("techno festival lineup", "bodybuilding workout", "youtube.com", "k-music-1", "s-fitness-1"):
        assert raw not in aggregate


def test_subject_apply_is_one_transaction_and_exact_retry_does_not_recount(tmp_path: Path) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    scan = _scan(source)
    review = build_reviewed_backfill(scan, secret=SECRET)
    store = ContactMemoryStore(tmp_path / "guest", "stephen-lucier")

    first = apply_subject_backfill(
        scan, review, subject="stephen-lucier", store=store,
        approved_review_id=review.manifest["review_id"], secret=SECRET,
    )
    before = store.list_interests()
    second = apply_subject_backfill(
        scan, review, subject="stephen-lucier", store=store,
        approved_review_id=review.manifest["review_id"], secret=SECRET,
    )

    assert first["already_applied"] is False
    assert first["projected_candidates"] == 2
    assert second["already_applied"] is True
    assert store.list_interests() == before
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT count(*) FROM import_run WHERE run_id LIKE 'reviewed-communication:%'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM communication_projection_receipt").fetchone()[0] == 4


def test_subject_apply_rejects_wrong_approval_and_rolls_back_mixed_projection_failure(tmp_path: Path) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    scan = _scan(source)
    review = build_reviewed_backfill(scan, secret=SECRET)
    store = ContactMemoryStore(tmp_path / "guest", "stephen-lucier")

    with pytest.raises(ValueError, match="exactly match"):
        apply_subject_backfill(
            scan, review, subject="stephen-lucier", store=store,
            approved_review_id="0" * 64, secret=SECRET,
        )
    with sqlite3.connect(store.path) as con:
        con.execute("""CREATE TRIGGER fail_entity BEFORE INSERT ON projected_entity
                       BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(ValueError):
        apply_subject_backfill(
            scan, review, subject="stephen-lucier", store=store,
            approved_review_id=review.manifest["review_id"], secret=SECRET,
        )
    with sqlite3.connect(store.path) as con:
        for table in ("communication_event", "interest_event", "projected_entity", "communication_projection_receipt", "import_run"):
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_restore_rehearsal_restores_exact_database_bytes_in_temporary_roots(tmp_path: Path) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    scan = _scan(source)
    review = build_reviewed_backfill(scan, secret=SECRET)
    store = ContactMemoryStore(tmp_path / "source-root", "stephen-lucier")
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()

    evidence = restore_rehearsal(
        scan, review, subject="stephen-lucier", source_store=store,
        rehearsal_root=tmp_path / "rehearsal", secret=SECRET,
    )

    assert evidence["restored_sha256"] == evidence["backup_sha256"]
    assert evidence["mutated_sha256"] != evidence["backup_sha256"]
    assert evidence["apply_projected_candidates"] == 2
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before


def test_cli_publishes_owner_only_review_and_requires_complete_apply_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    fake_home = tmp_path / "home"
    key = tmp_path / "key"
    key.write_bytes(SECRET)
    key.chmod(0o600)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    artifacts = fake_home / ".hermes/profiles/coding/artifacts/review"
    args = [
        "--chat-db", str(source), "--handle", "steve@example.test",
        "--hmac-key", str(key), "--artifact-root", str(artifacts),
    ]

    assert cli_main(args) == 0
    for path in (
        artifacts / "aggregate-review-manifest.json",
        artifacts / "candidate-summary.json",
        artifacts / "private-source-evidence.json",
        artifacts / "rehearsal-evidence.json",
    ):
        assert path.is_file() and path.stat().st_mode & 0o077 == 0
    with pytest.raises(SystemExit) as exc:
        cli_main([*args, "--apply"])
    assert exc.value.code == 2
