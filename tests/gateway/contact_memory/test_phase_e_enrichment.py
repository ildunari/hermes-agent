from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from typing import Sequence

from gateway.contact_memory.phase_e_taxonomy import PLATFORM_ENTITY_DENYLIST
from gateway.contact_memory.reviewed_backfill import (
    apply_subject_backfill,
    build_candidate_selection,
    build_reviewed_backfill,
    subject_review,
    verify_candidate_selection,
    verify_subject_review,
)
from gateway.contact_memory.store import ContactMemoryStore
from tests.gateway.contact_memory.test_reviewed_communication_backfill import (
    DAY,
    SECRET,
    _messages,
    _scan,
)


def _replace_messages(path: Path, rows: Sequence[tuple[object, ...]]) -> None:
    con = sqlite3.connect(path)
    con.execute("DELETE FROM chat_message_join")
    con.execute("DELETE FROM message")
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO chat_message_join VALUES(1,?)", [(row[0],) for row in rows])
    con.commit()
    con.close()


def test_multilabel_longest_phrase_and_closed_public_entities(tmp_path: Path) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    _replace_messages(source, [
        (1, "a", 1 * DAY, 0, "I love movie night and cardio workout", None, 0, None, None, 1),
        (2, "b", 3 * DAY, 0, "I love another movie night and cardio workout", None, 0, None, None, 1),
        (3, "c", 5 * DAY, 0, "Lady Gaga news", None, 0, None, None, 1),
        (4, "d", 7 * DAY, 0, "Lady Gaga update", None, 0, None, None, 1),
    ])

    review = build_reviewed_backfill(_scan(source), secret=SECRET)
    stephen = subject_review(review, "stephen-lucier")
    labels = {item["label"] for item in stephen["candidates"]}

    assert {"movies", "cardio training", "training sessions", "Lady Gaga"} <= labels
    assert all(item["label"].casefold() not in PLATFORM_ENTITY_DENYLIST for item in stephen["candidates"])
    assert all(item["eligibility"] == "eligible" for item in stephen["candidates"])
    assert review.manifest["evaluation"]["platform_entity_candidates"] == 0


def test_questions_third_party_logistics_sarcasm_and_mentions_are_watchlist_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    rows = [
        (1, "q1", 1 * DAY, 0, "Do you like movies?", None, 0, None, None, 1),
        (2, "q2", 3 * DAY, 0, "Would you watch a movie?", None, 0, None, None, 1),
        (3, "t1", 5 * DAY, 0, "My friend loves travel", None, 0, None, None, 1),
        (4, "t2", 7 * DAY, 0, "She loves another trip", None, 0, None, None, 1),
        (5, "l1", 9 * DAY, 0, "I need to get car service appointment", None, 0, None, None, 1),
        (6, "l2", 11 * DAY, 0, "I have to get vehicle service", None, 0, None, None, 1),
        (7, "s1", 13 * DAY, 0, "I love cardio lol", None, 0, None, None, 1),
        (8, "s2", 15 * DAY, 0, "I love cardio lmao", None, 0, None, None, 1),
        (9, "h1", 17 * DAY, 0, "at the house", None, 0, None, None, 1),
        (10, "h2", 19 * DAY, 0, "house keys", None, 0, None, None, 1),
    ]
    _replace_messages(source, rows)

    review = build_reviewed_backfill(_scan(source), secret=SECRET)
    stephen = subject_review(review, "stephen-lucier")
    assert stephen["candidates"] == []
    watchlist = review.manifest["watchlist"]
    assert {item["reason"] for item in watchlist} >= {"gated_context"}
    assert "electronic dance music" not in {item["label"] for item in watchlist}


def test_batch_collapse_requires_two_independent_recurrence_units(tmp_path: Path) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    _replace_messages(source, [
        (1, "a", 1 * DAY, 0, "I love cardio", None, 0, None, None, 1),
        (2, "b", 1 * DAY + 1_000_000_000, 0, "I love cardio workout", None, 0, None, None, 1),
    ])
    review = build_reviewed_backfill(_scan(source), secret=SECRET)
    assert subject_review(review, "stephen-lucier")["candidates"] == []
    assert review.manifest["exclusions"]["watchlist_insufficient_recurrence"] >= 1


def test_support_derivation_and_candidate_subset_are_signed_and_tamper_evident(
    tmp_path: Path,
) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    review = build_reviewed_backfill(_scan(source), secret=SECRET)
    snapshot = subject_review(review, "stephen-lucier")
    ids = [item["candidate_id"] for item in snapshot["candidates"][:2]]
    selection = build_candidate_selection(
        review, subject="stephen-lucier", candidate_ids=ids, secret=SECRET,
    )

    assert verify_candidate_selection(selection, review, secret=SECRET)
    occurrence = snapshot["candidates"][0]["occurrences"][0]
    assert all(key in occurrence for key in (
        "support_id", "recurrence_support_id", "actor_code", "polarity",
        "rule_code", "ontology_version", "derivation_version",
        "derivation_commitment", "projection",
    ))
    changed_selection = deepcopy(selection)
    changed_selection["candidates"][0]["candidate_payload_commitment"] = "0" * 64
    assert not verify_candidate_selection(changed_selection, review, secret=SECRET)
    changed_review = deepcopy(review)
    changed_snapshot = subject_review(changed_review, "stephen-lucier")
    changed_snapshot["candidates"][0]["occurrences"][0]["support_id"] = "1" * 64
    assert not verify_subject_review(changed_snapshot, secret=SECRET)


def test_reply_and_reaction_support_require_authenticated_counterpart_semantics(
    tmp_path: Path,
) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    _replace_messages(source, [
        (1, "owner-movie-1", 1 * DAY, 1, "I love movies", None, 0, None, None, None),
        (2, "reply-1", 2 * DAY, 0, "I love that too", None, 0, None, "p:0/owner-movie-1", 1),
        (3, "owner-movie-2", 3 * DAY, 1, "I love another movie", None, 0, None, None, None),
        (4, "reply-2", 4 * DAY, 0, "I love that too", None, 0, None, "p:0/owner-movie-2", 1),
        (5, "owner-cardio-1", 5 * DAY, 1, "I love cardio", None, 0, None, None, None),
        (6, "react-1", 6 * DAY, 0, "Loved", None, 2000, "p:0/owner-cardio-1", None, 1),
        (7, "owner-cardio-2", 7 * DAY, 1, "I love more cardio", None, 0, None, None, None),
        (8, "react-2", 8 * DAY, 0, "Liked", None, 2001, "p:0/owner-cardio-2", None, 1),
    ])
    scan = _scan(source)
    review = build_reviewed_backfill(scan, secret=SECRET)
    snapshot = subject_review(review, "stephen-lucier")
    selected = [
        item for item in snapshot["candidates"]
        if item["label"] in {"movies", "cardio training"}
    ]

    assert {item["label"] for item in selected} == {"movies", "cardio training"}
    assert all(
        occurrence["authenticated_target"] is True
        for item in selected for occurrence in item["occurrences"]
    )
    store = ContactMemoryStore(tmp_path / "guest", "stephen-lucier")
    result = apply_subject_backfill(
        scan, review, subject="stephen-lucier", store=store,
        approved_subject_review_id=snapshot["subject_review_id"],
        approved_candidate_ids=[item["candidate_id"] for item in selected],
        secret=SECRET,
    )
    assert result["projected_candidates"] == 2


def test_privacy_canaries_never_enter_aggregate_manifest_or_summary(tmp_path: Path) -> None:
    source = tmp_path / "chat.db"
    _messages(source)
    canaries = (
        "RAW-CANARY-secret-message", "https://private.invalid/token/abc",
        "/Users/private/Library/secret", "ABCDEF12-3456-7890-ABCD-EF1234567890",
    )
    con = sqlite3.connect(source)
    for offset, canary in enumerate(canaries, start=20):
        con.execute(
            "INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?,?)",
            (offset, f"private-source-{offset}", offset * DAY, 0, canary, None, 0, None, None, 1),
        )
        con.execute("INSERT INTO chat_message_join VALUES(1,?)", (offset,))
    con.commit()
    con.close()

    review = build_reviewed_backfill(_scan(source), secret=SECRET)
    aggregate = json.dumps(review.manifest, sort_keys=True)
    assert all(canary not in aggregate for canary in canaries)
    assert "private-source-" not in aggregate
    assert "://" not in aggregate
