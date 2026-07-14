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
            "fact_id": "kosta_ai_tech", "fact_type": "preference",
            "fact_value": "Kosta likes local AI tools. Extra context is not the topic.",
            "source_record_id": "k1", "sensitivity": "normal", "needs_review": False,
        },
        {
            "fact_type": "preference", "fact_value": "Stephen prefers oversized clothes.",
            "source_record_id": "1464A261-B77B-48A2-A9A9-6A0ECAFCB485", "sensitivity": "normal", "needs_review": False,
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
        {"evidence_id": "es", "source_record_id": "1464A261-B77B-48A2-A9A9-6A0ECAFCB485", "speaker": "contact/handle",
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
    from scripts import import_reviewed_artifact_interests as converter
    for raw in facts.read_bytes().splitlines():
        row = json.loads(raw)
        key = str(row.get("fact_id") or row.get("source_record_id"))
        if key in {"kosta_ai_tech", "1464A261-B77B-48A2-A9A9-6A0ECAFCB485"}:
            converter._CURATED_FACT_SHA256[key] = hashlib.sha256(raw).hexdigest()
    return facts, evidence


def test_converter_is_deterministic_strictly_attributed_and_excludes_unsafe_material(tmp_path: Path):
    facts, evidence = _artifacts(tmp_path)
    batches, manifest = build_batches(facts, evidence)
    batches_again, manifest_again = build_batches(facts, evidence)

    assert manifest == manifest_again
    assert _canonical_bytes(manifest) == _canonical_bytes(manifest_again)
    assert [event.topic_text for event in batches["kosta-owner"]] == ["AI agent systems"]
    assert [event.topic_text for event in batches["stephen-lucier"]] == ["oversized gym clothes"]
    assert batches == batches_again
    assert manifest["counts"]["accepted_topics"] == 2
    assert manifest["counts"]["accepted_events"] == 2
    assert manifest["counts"]["excluded"]["artifact_sensitive_or_unreviewed"] == 1
    assert manifest["counts"]["excluded"]["not_curated_for_proactive"] >= 1
    assert all(event.valence.value == "positive" for events in batches.values() for event in events)
    assert all("evidence_pointer" in event for event in manifest["events"])


def test_curated_key_cannot_override_contradictory_subject_text(tmp_path: Path):
    facts, evidence = _artifacts(tmp_path)
    rows = [json.loads(line) for line in facts.read_text().splitlines()]
    rows[0]["fact_value"] = "Stephen likes local AI tools."
    _write_jsonl(facts, rows)

    batches, manifest = build_batches(facts, evidence)

    assert batches["kosta-owner"] == []
    assert manifest["counts"]["excluded"]["curated_fact_changed"] == 1


def test_dry_run_writes_private_review_only_and_apply_requires_exact_integrity_hash(tmp_path: Path):
    facts, evidence = _artifacts(tmp_path)
    review = tmp_path / "review.json"
    assert main(["--facts", str(facts), "--evidence", str(evidence), "--review-manifest", str(review)]) == 0
    payload = review.read_bytes()
    assert not (review.stat().st_mode & 0o077)
    assert json.loads(payload)["counts"]["accepted_topics"] == 2

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
    assert [event.topic for event in ContactMemoryStore(
        roots["kosta-owner"] / "contact-memory", "kosta-owner"
    ).eligible_interests(now=1767373200)] == ["ai agent systems"]
    assert [event.topic for event in ContactMemoryStore(
        roots["stephen-lucier"] / "contact-memory", "stephen-lucier"
    ).eligible_interests(now=1767373200)] == ["oversized gym clothes"]

    poke_store = ContactMemoryStore(roots["kosta-owner"] / "contact-memory", "kosta-owner")
    guest_store = ContactMemoryStore(roots["stephen-lucier"] / "contact-memory", "stephen-lucier")
    poke_before = poke_store.list_interests()[0].updated_at
    guest_before = guest_store.list_interests()[0].updated_at
    assert main([*arguments, "--apply", "--approved-review-sha256", digest]) == 0
    assert len(poke_store.list_interests()) == 1
    assert len(guest_store.list_interests()) == 1
    assert poke_store.list_interests()[0].updated_at == poke_before
    assert guest_store.list_interests()[0].updated_at == guest_before

    import sqlite3
    for store, own, forbidden in (
        (poke_store, "kosta-owner", "stephen-lucier"),
        (guest_store, "stephen-lucier", "kosta-owner"),
    ):
        with sqlite3.connect(store.path) as con:
            payload = con.execute(
                "SELECT manifest_json FROM import_run WHERE run_id LIKE 'reviewed-artifact-interests:%'"
            ).fetchone()[0]
        assert json.loads(payload)["contact_id"] == own
        assert forbidden not in payload
