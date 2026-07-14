from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
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
    subject_review,
    verify_review_manifest,
    verify_subject_review,
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


def _approval(review, subject: str, *, kinds: set[str] | None = None):
    snapshot = subject_review(review, subject)
    candidate_ids = [
        item["candidate_id"] for item in snapshot["candidates"]
        if kinds is None or item["kind"] in kinds
    ]
    return snapshot, candidate_ids


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
    assert verify_review_manifest(first.manifest, secret=SECRET)
    assert first.manifest["schema"] == 2
    assert first.manifest["global_review_id"] != (
        "8ce521b7cde167ffc581f6b1b639895de84f46b955e42affd51b72041b2aafb1"
    )
    assert all(
        verify_subject_review(item, secret=SECRET)
        for item in first.manifest["subject_reviews"]
    )
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
    snapshot, candidate_ids = _approval(review, "stephen-lucier")

    first = apply_subject_backfill(
        scan, review, subject="stephen-lucier", store=store,
        approved_subject_review_id=snapshot["subject_review_id"],
        approved_candidate_ids=candidate_ids, secret=SECRET,
    )
    before = store.list_interests()
    second = apply_subject_backfill(
        scan, review, subject="stephen-lucier", store=store,
        approved_subject_review_id=snapshot["subject_review_id"],
        approved_candidate_ids=candidate_ids, secret=SECRET,
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
    snapshot, candidate_ids = _approval(review, "stephen-lucier")

    with pytest.raises(ValueError, match="exactly match"):
        apply_subject_backfill(
            scan, review, subject="stephen-lucier", store=store,
            approved_subject_review_id="0" * 64,
            approved_candidate_ids=candidate_ids, secret=SECRET,
        )
    with sqlite3.connect(store.path) as con:
        con.execute("""CREATE TRIGGER fail_entity BEFORE INSERT ON projected_entity
                       BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(ValueError):
        apply_subject_backfill(
            scan, review, subject="stephen-lucier", store=store,
            approved_subject_review_id=snapshot["subject_review_id"],
            approved_candidate_ids=candidate_ids, secret=SECRET,
        )
    with sqlite3.connect(store.path) as con:
        for table in ("communication_event", "interest_event", "projected_entity", "communication_projection_receipt", "import_run"):
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_candidate_allowlist_imports_only_selected_occurrences_and_rejects_cross_subject(
    tmp_path: Path,
) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    scan = _scan(source)
    review = build_reviewed_backfill(scan, secret=SECRET)
    store = ContactMemoryStore(tmp_path / "guest", "stephen-lucier")
    snapshot, topic_ids = _approval(review, "stephen-lucier", kinds={"topic"})
    _kosta_snapshot, kosta_ids = _approval(review, "kosta-owner")

    with pytest.raises(ValueError, match="unknown or belongs"):
        apply_subject_backfill(
            scan, review, subject="stephen-lucier", store=store,
            approved_subject_review_id=snapshot["subject_review_id"],
            approved_candidate_ids=kosta_ids, secret=SECRET,
        )
    result = apply_subject_backfill(
        scan, review, subject="stephen-lucier", store=store,
        approved_subject_review_id=snapshot["subject_review_id"],
        approved_candidate_ids=topic_ids, secret=SECRET,
    )

    assert result["projected_candidates"] == 1
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT count(*) FROM interest_event").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM projected_entity").fetchone()[0] == 0
        stored = json.loads(con.execute("SELECT manifest_json FROM import_run").fetchone()[0])
    assert stored["candidate_ids"] == topic_ids
    assert all(item["kind"] == "topic" for item in stored["candidates"])
    with pytest.raises(ValueError, match="target snapshot is stale"):
        apply_subject_backfill(
            scan, review, subject="stephen-lucier", store=store,
            approved_subject_review_id=snapshot["subject_review_id"],
            approved_candidate_ids=[
                item["candidate_id"] for item in snapshot["candidates"]
            ], secret=SECRET,
        )
    for invalid in ([], [topic_ids[0], topic_ids[0]]):
        fresh_store = ContactMemoryStore(tmp_path / f"invalid-{len(invalid)}", "stephen-lucier")
        with pytest.raises(ValueError, match="non-empty duplicate-free"):
            apply_subject_backfill(
                scan, review, subject="stephen-lucier", store=fresh_store,
                approved_subject_review_id=snapshot["subject_review_id"],
                approved_candidate_ids=invalid, secret=SECRET,
            )


def test_projection_tamper_and_retired_review_id_fail_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    scan = _scan(source)
    review = build_reviewed_backfill(scan, secret=SECRET)
    snapshot, candidate_ids = _approval(review, "stephen-lucier")
    entity_index = next(
        index for index, item in enumerate(snapshot["candidates"]) if item["kind"] == "entity"
    )
    topic_index = next(
        index for index, item in enumerate(snapshot["candidates"]) if item["kind"] == "topic"
    )
    mutations = [
        (entity_index, ["semantic_key"], "tampered-key"),
        (entity_index, ["occurrences", 0, "event_id"], "0" * 64),
        (entity_index, ["occurrences", 0, "source_id"], "1" * 64),
        (entity_index, ["occurrences", 0, "projection", "entities", 0, "canonical_label"], "Other"),
        (entity_index, ["occurrences", 0, "projection", "entities", 0, "entity_type"], "person"),
        (entity_index, ["occurrences", 0, "projection", "entities", 0, "confidence"], 0.5),
        (entity_index, ["occurrences", 0, "projection", "entities", 0, "source_method"], "model"),
        (topic_index, ["occurrences", 0, "projection", "interests", 0, "topic"], "travel"),
        (topic_index, ["occurrences", 0, "projection", "interests", 0, "signal_type"], "enthusiasm"),
        (topic_index, ["occurrences", 0, "projection", "interests", 0, "valence"], "negative"),
        (topic_index, ["occurrences", 0, "projection", "interests", 0, "confidence"], 0.25),
        (topic_index, ["occurrences", 0, "projection", "interests", 0, "source_method"], "model"),
    ]
    for candidate_index, path, value in mutations:
        changed = deepcopy(review)
        changed_snapshot = subject_review(changed, "stephen-lucier")
        target = changed_snapshot["candidates"][candidate_index]
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        assert not verify_subject_review(changed_snapshot, secret=SECRET)
    tampered = deepcopy(review)
    tampered_snapshot = subject_review(tampered, "stephen-lucier")
    tampered_snapshot["candidates"][0]["occurrences"][0]["projection"]["entities"][0][
        "entity_type"
    ] = "person"
    assert not verify_subject_review(tampered_snapshot, secret=SECRET)
    store = ContactMemoryStore(tmp_path / "guest", "stephen-lucier")

    with pytest.raises(ValueError, match="HMAC"):
        apply_subject_backfill(
            scan, tampered, subject="stephen-lucier", store=store,
            approved_subject_review_id=snapshot["subject_review_id"],
            approved_candidate_ids=candidate_ids, secret=SECRET,
        )
    with pytest.raises(ValueError, match="retired"):
        apply_subject_backfill(
            scan, review, subject="stephen-lucier", store=store,
            approved_subject_review_id=(
                "8ce521b7cde167ffc581f6b1b639895de84f46b955e42affd51b72041b2aafb1"
            ), approved_candidate_ids=candidate_ids, secret=SECRET,
        )
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT count(*) FROM communication_event").fetchone()[0] == 0


def test_subject_snapshots_allow_sequential_cross_subject_applies(tmp_path: Path) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    scan = _scan(source)
    review = build_reviewed_backfill(scan, secret=SECRET)
    kosta_store = ContactMemoryStore(tmp_path / "poke", "kosta-owner")
    stephen_store = ContactMemoryStore(tmp_path / "guest", "stephen-lucier")
    kosta_snapshot, kosta_ids = _approval(review, "kosta-owner")
    stephen_snapshot, stephen_ids = _approval(review, "stephen-lucier")

    apply_subject_backfill(
        scan, review, subject="kosta-owner", store=kosta_store,
        approved_subject_review_id=kosta_snapshot["subject_review_id"],
        approved_candidate_ids=kosta_ids, secret=SECRET,
    )
    result = apply_subject_backfill(
        scan, review, subject="stephen-lucier", store=stephen_store,
        approved_subject_review_id=stephen_snapshot["subject_review_id"],
        approved_candidate_ids=stephen_ids, secret=SECRET,
    )
    assert result["already_applied"] is False
    assert result["projected_candidates"] == 2


def test_apply_rejects_stale_subject_target_and_source_without_partial_writes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    scan = _scan(source)
    review = build_reviewed_backfill(scan, secret=SECRET)
    snapshot, candidate_ids = _approval(review, "stephen-lucier")
    store = ContactMemoryStore(tmp_path / "guest", "stephen-lucier")
    unrelated = next(record.bundle for record in scan.records if record.author == "stephen-lucier")
    store.ingest_communication_event(
        unrelated.event, urls=unrelated.urls, attachments=unrelated.attachments,
        relations=unrelated.relations, entity_mentions=unrelated.entity_mentions,
        recommendation_events=unrelated.recommendation_events,
    )
    with pytest.raises(ValueError, match="target snapshot is stale"):
        apply_subject_backfill(
            scan, review, subject="stephen-lucier", store=store,
            approved_subject_review_id=snapshot["subject_review_id"],
            approved_candidate_ids=candidate_ids, secret=SECRET,
        )
    changed_scan = replace(scan, records=tuple(
        record for index, record in enumerate(scan.records)
        if index != next(
            item for item, candidate in enumerate(scan.records)
            if candidate.author == "stephen-lucier"
        )
    ))
    clean_store = ContactMemoryStore(tmp_path / "clean-guest", "stephen-lucier")
    with pytest.raises(ValueError, match="source scan is stale"):
        apply_subject_backfill(
            changed_scan, review, subject="stephen-lucier", store=clean_store,
            approved_subject_review_id=snapshot["subject_review_id"],
            approved_candidate_ids=candidate_ids, secret=SECRET,
        )
    with sqlite3.connect(clean_store.path) as con:
        assert con.execute("SELECT count(*) FROM communication_event").fetchone()[0] == 0


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
