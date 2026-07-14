from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gateway.contact_memory.store import ContactMemoryStore
from scripts.import_reviewed_artifact_interests import (
    _canonical_bytes,
    build_batches,
    main,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _artifacts(tmp_path: Path) -> tuple[Path, Path]:
    facts = tmp_path / "facts.jsonl"
    evidence = tmp_path / "evidence.jsonl"
    _write_jsonl(facts, [
        {
            "fact_id": "kosta_ai_tools", "fact_type": "preference",
            "fact_value": "Kosta likes local AI tools. Extra context is not the topic.",
            "source_record_id": "k1", "sensitivity": "normal", "needs_review": False,
        },
        {
            "fact_type": "preference", "fact_value": "Stephen prefers oversized clothes.",
            "source_record_id": "s1", "sensitivity": "normal", "needs_review": False,
        },
        {
            "fact_id": "kosta_wrong_speaker", "fact_type": "preference",
            "fact_value": "Kosta likes jazz.", "source_record_id": "wrong",
            "sensitivity": "normal", "needs_review": False,
        },
        {
            "fact_id": "kosta_drugs", "fact_type": "preference",
            "fact_value": "Kosta likes games while stoned.", "source_record_id": "drug",
            "sensitivity": "normal", "needs_review": False,
        },
        {
            "fact_id": "stephen_alcohol", "fact_type": "preference",
            "fact_value": "Stephen usually drinks tequila.", "source_record_id": "alcohol",
            "sensitivity": "normal", "needs_review": False,
        },
        {
            "fact_id": "kosta_true_crime", "fact_type": "preference",
            "fact_value": "Kosta likes true crime podcasts.", "source_record_id": "crime",
            "sensitivity": "normal", "needs_review": False,
        },
        {
            "fact_id": "kosta_shared", "fact_type": "preference",
            "fact_value": "Kosta likes hiking with Stephen.", "source_record_id": "third",
            "sensitivity": "normal", "needs_review": False,
        },
        {
            "fact_id": "kosta_ambiguous", "fact_type": "preference",
            "fact_value": "Kosta likes fall.", "source_record_id": "both",
            "sensitivity": "normal", "needs_review": False,
        },
        {
            "fact_id": "kosta_health", "fact_type": "preference",
            "fact_value": "Kosta likes science.", "source_record_id": "health",
            "sensitivity": "sensitive", "needs_review": True,
        },
    ])
    _write_jsonl(evidence, [
        {"evidence_id": "ek", "source_record_id": "k1", "speaker": "Kosta",
         "timestamp": "2026-01-01T12:00:00-05:00", "summary": "Kosta discusses local AI tools"},
        {"evidence_id": "es", "source_record_id": "s1", "speaker": "contact/handle",
         "timestamp": "2026-01-02T12:00:00-05:00", "summary": "Stephen prefers oversized clothes"},
        {"source_record_id": "wrong", "speaker": "Stephen", "timestamp": "2026-01-03T00:00:00Z",
         "summary": "Kosta likes jazz"},
        {"source_record_id": "drug", "speaker": "Kosta", "timestamp": "2026-01-04T00:00:00Z",
         "summary": "Kosta likes games"},
        {"source_record_id": "alcohol", "speaker": "Stephen", "timestamp": "2026-01-04T01:00:00Z",
         "summary": "Stephen drinks tequila"},
        {"source_record_id": "crime", "speaker": "Kosta", "timestamp": "2026-01-04T02:00:00Z",
         "summary": "Kosta likes true crime"},
        {"source_record_id": "third", "speaker": "Kosta", "timestamp": "2026-01-05T00:00:00Z",
         "summary": "Hiking"},
        {"source_record_id": "both", "speaker": "Kosta/Stephen", "timestamp": "2026-01-06T00:00:00Z",
         "summary": "Fall"},
        {"source_record_id": "health", "speaker": "Kosta", "timestamp": "2026-01-07T00:00:00Z",
         "summary": "Science"},
    ])
    return facts, evidence


def test_converter_is_deterministic_strictly_attributed_and_excludes_unsafe_material(tmp_path: Path):
    facts, evidence = _artifacts(tmp_path)
    batches, manifest = build_batches(facts, evidence)
    batches_again, manifest_again = build_batches(facts, evidence)

    assert manifest == manifest_again
    assert _canonical_bytes(manifest) == _canonical_bytes(manifest_again)
    assert [event.topic_text for event in batches["kosta-owner"]] == ["Kosta likes local AI tools."]
    assert [event.topic_text for event in batches["stephen-lucier"]] == ["Stephen prefers oversized clothes."]
    assert batches == batches_again
    assert manifest["counts"]["accepted"] == 2
    assert manifest["counts"]["excluded"] == {
        "ambiguous_speaker": 1,
        "artifact_sensitive_or_unreviewed": 1,
        "fact_subject_mismatch": 1,
        "sensitive_topic": 3,
        "third_party_material": 1,
    }
    assert all(event.valence.value == "positive" for events in batches.values() for event in events)
    assert all("evidence_pointer" in event for event in manifest["events"])


def test_dry_run_writes_private_review_only_and_apply_requires_exact_integrity_hash(tmp_path: Path):
    facts, evidence = _artifacts(tmp_path)
    review = tmp_path / "review.json"
    assert main(["--facts", str(facts), "--evidence", str(evidence), "--review-manifest", str(review)]) == 0
    payload = review.read_bytes()
    assert not (review.stat().st_mode & 0o077)
    assert json.loads(payload)["counts"]["accepted"] == 2

    with pytest.raises(SystemExit) as exc:
        main([
            "--facts", str(facts), "--evidence", str(evidence), "--review-manifest", str(review),
            "--apply", "--approved-review-sha256", "0" * 64,
        ])
    assert exc.value.code == 2
    assert not (tmp_path / "poke").exists()
    assert not (tmp_path / "guest").exists()


def test_integrity_checked_cli_uses_atomic_typed_import_in_isolated_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from hermes_cli import profiles

    facts, evidence = _artifacts(tmp_path)
    review = tmp_path / "review.json"
    roots = {"kosta-owner": tmp_path / "poke", "stephen-lucier": tmp_path / "guest"}
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: roots[
        "kosta-owner" if name == "poke" else "stephen-lucier"
    ])
    arguments = ["--facts", str(facts), "--evidence", str(evidence), "--review-manifest", str(review)]
    assert main(arguments) == 0
    digest = hashlib.sha256(review.read_bytes()).hexdigest()

    assert main([*arguments, "--apply", "--approved-review-sha256", digest]) == 0
    assert [event.topic_text for event in ContactMemoryStore(
        roots["kosta-owner"] / "contact-memory", "kosta-owner"
    ).unfolded_interest_events()] == ["Kosta likes local AI tools."]
    assert [event.topic_text for event in ContactMemoryStore(
        roots["stephen-lucier"] / "contact-memory", "stephen-lucier"
    ).unfolded_interest_events()] == ["Stephen prefers oversized clothes."]

    assert main([*arguments, "--apply", "--approved-review-sha256", digest]) == 0
    assert len(ContactMemoryStore(
        roots["kosta-owner"] / "contact-memory", "kosta-owner"
    ).unfolded_interest_events()) == 1
    assert len(ContactMemoryStore(
        roots["stephen-lucier"] / "contact-memory", "stephen-lucier"
    ).unfolded_interest_events()) == 1
