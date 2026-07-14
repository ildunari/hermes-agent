from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from gateway.contact_memory.imessage_bootstrap import ResolvedChat
from gateway.contact_memory.imessage_link_review import LinkSignal, build_review_manifest, evidence_id
from gateway.contact_memory.schema import InterestValence, SignalType
from gateway.contact_memory.store import ContactMemoryStore
from scripts.apply_reviewed_link_interests import main, validate_manifest

_SECRET = b"review-bridge-test-secret-32bytes!"
_CHAT = ResolvedChat(1, "iMessage;-;+14015550100", 2, "+14015550100", 2, (2,))


def _manifest(*, include_tech: bool = False) -> dict:
    signals = [
        LinkSignal("https://example.com/one", "https://example.com/one", "g1", 1_735_689_600,
                   "stephen-lucier", "example.com", None),
        LinkSignal("https://example.com/two", "https://example.com/two", "g2", 1_738_368_000,
                   "stephen-lucier", "example.com", None),
    ]
    metadata = {}
    for signal in signals:
        text = "music song" + (" software coding" if include_tech else "")
        metadata[evidence_id(_SECRET, "url", signal.identity_url)] = {"title": text}
    return build_review_manifest(_CHAT, signals, secret=_SECRET, metadata_cache=metadata)


def _resign(manifest: dict) -> dict:
    manifest.pop("review_id", None)
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["review_id"] = evidence_id(_SECRET, "review", payload)
    return manifest


def _private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _files(tmp_path: Path, manifest: dict | None = None) -> tuple[Path, Path]:
    review, key = tmp_path / "review.json", tmp_path / "key"
    _private(review, (json.dumps(manifest or _manifest()) + "\n").encode())
    _private(key, _SECRET)
    return review, key


def test_valid_manifest_creates_stable_minimal_events_at_latest_month() -> None:
    manifest = _manifest()
    event = validate_manifest(manifest, _SECRET, manifest["review_id"])["stephen-lucier"][0]
    assert event.signal_type is SignalType.ENGAGED_MENTION
    assert event.valence is InterestValence.POSITIVE
    assert event.event_id == manifest["candidates"][0]["candidate_id"]
    assert event.source_id == f"reviewed_link_manifest:candidate:{event.event_id}"
    assert event.created_at == datetime(2025, 2, 1, tzinfo=timezone.utc).timestamp()
    assert "http" not in event.source_id and "g1" not in event.source_id


@pytest.mark.parametrize("mutation, match", [
    (lambda m: m.update({"extra": True}), "fields are invalid"),
    (lambda m: m["candidates"][0].update(subject="someone-else"), "unsupported candidate subject"),
    (lambda m: m["candidates"][0].update(topic="unknown interest"), "outside the link taxonomy"),
    (lambda m: m["candidates"][0].update(distinct_evidence=1, evidence_ids=m["candidates"][0]["evidence_ids"][:1]), "insufficient evidence"),
    (lambda m: m["candidates"][0].update(month_buckets=["2025-13"]), "strict YYYY-MM"),
])
def test_strict_schema_subject_taxonomy_strength_and_month_validation(mutation, match: str) -> None:
    manifest = deepcopy(_manifest())
    mutation(manifest)
    _resign(manifest)
    with pytest.raises(ValueError, match=match):
        validate_manifest(manifest, _SECRET, manifest["review_id"])


def test_positive_reaction_allows_one_distinct_evidence() -> None:
    manifest = deepcopy(_manifest())
    candidate = manifest["candidates"][0]
    candidate["distinct_evidence"] = 1
    candidate["evidence_ids"] = candidate["evidence_ids"][:1]
    candidate["positive_reactions"] = 1
    _resign(manifest)
    assert len(validate_manifest(manifest, _SECRET)["stephen-lucier"]) == 1


def test_producer_enforces_strength_per_actor() -> None:
    signals = [
        LinkSignal("https://example.com/s", "https://example.com/s", "s", 1_735_689_600,
                   "stephen-lucier", "example.com", None),
        LinkSignal("https://example.com/k", "https://example.com/k", "k", 1_735_689_600,
                   "kosta-owner", "example.com", None),
    ]
    metadata = {
        evidence_id(_SECRET, "url", signal.identity_url): {"title": "music song"}
        for signal in signals
    }
    manifest = build_review_manifest(_CHAT, signals, secret=_SECRET, metadata_cache=metadata)
    assert manifest["candidates"] == []
    assert manifest["counts"]["excluded"]["insufficient_actor_evidence"] == 2


def test_multi_subject_apply_requires_explicit_atomic_subject(tmp_path: Path) -> None:
    signals = [
        LinkSignal(f"https://example.com/{actor}/{index}", f"https://example.com/{actor}/{index}",
                   f"{actor}-{index}", 1_735_689_600, actor, "example.com", None)
        for actor in ("kosta-owner", "stephen-lucier") for index in range(2)
    ]
    metadata = {
        evidence_id(_SECRET, "url", signal.identity_url): {"title": "music song"}
        for signal in signals
    }
    manifest = build_review_manifest(_CHAT, signals, secret=_SECRET, metadata_cache=metadata)
    review, key = _files(tmp_path, manifest)
    args = [
        "--review-manifest", str(review), "--hmac-key", str(key),
        "--approved-review-id", manifest["review_id"],
        "--poke-root", str(tmp_path / "poke"), "--guest-root", str(tmp_path / "guest"),
        "--apply",
    ]
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    assert not (tmp_path / "poke" / "contact-memory").exists()
    assert not (tmp_path / "guest" / "contact-memory").exists()


def test_hash_and_exact_approval_mismatch_are_rejected() -> None:
    manifest = _manifest()
    tampered = deepcopy(manifest)
    tampered["candidates"][0]["month_buckets"] = ["2026-01"]
    with pytest.raises(ValueError, match="HMAC verification failed"):
        validate_manifest(tampered, _SECRET)
    with pytest.raises(ValueError, match="does not exactly match"):
        validate_manifest(manifest, _SECRET, "0" * 64)


def test_cli_rejects_non_private_manifest_or_key(tmp_path: Path) -> None:
    review, key = _files(tmp_path)
    review.chmod(0o644)
    with pytest.raises(SystemExit) as exc:
        main(["--review-manifest", str(review), "--hmac-key", str(key)])
    assert exc.value.code == 2
    review.chmod(0o600)
    key.chmod(0o640)
    with pytest.raises(SystemExit) as exc:
        main(["--review-manifest", str(review), "--hmac-key", str(key)])
    assert exc.value.code == 2


def test_dry_run_never_resolves_roots_or_creates_live_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    review, key = _files(tmp_path)
    from scripts import apply_reviewed_link_interests as bridge
    monkeypatch.setattr(bridge, "_default_roots", lambda: pytest.fail("dry run touched profile state"))
    before = set(tmp_path.iterdir())
    assert main(["--review-manifest", str(review), "--hmac-key", str(key)]) == 0
    assert set(tmp_path.iterdir()) == before


def _apply_args(review: Path, key: Path, root: Path, review_id: str) -> list[str]:
    return [
        "--review-manifest", str(review), "--hmac-key", str(key),
        "--approved-review-id", review_id, "--guest-root", str(root),
        "--poke-root", str(root / "poke-unused"), "--apply",
    ]


def test_apply_is_idempotent_and_new_review_expands_without_refreshing_old_topic(tmp_path: Path) -> None:
    root = tmp_path / "guest"
    first = _manifest()
    review, key = _files(tmp_path, first)
    assert main(_apply_args(review, key, root, first["review_id"])) == 0
    store = ContactMemoryStore(root / "contact-memory", "stephen-lucier")
    old = store.list_interests()[0]
    assert main(_apply_args(review, key, root, first["review_id"])) == 0
    assert store.list_interests()[0].updated_at == old.updated_at

    expanded = _manifest(include_tech=True)
    _private(review, (json.dumps(expanded) + "\n").encode())
    assert main(_apply_args(review, key, root, expanded["review_id"])) == 0
    interests = {item.topic: item for item in store.list_interests()}
    assert set(interests) == {"music", "technology"}
    assert interests["music"].updated_at == old.updated_at
    assert interests["music"].evidence_count == old.evidence_count


def test_atomic_conflict_rolls_back_entire_subject_batch(tmp_path: Path) -> None:
    manifest = _manifest(include_tech=True)
    events = validate_manifest(manifest, _SECRET)["stephen-lucier"]
    store = ContactMemoryStore(tmp_path / "contact-memory", "stephen-lucier")
    conflict = events[-1]
    store.record_interest_event(
        topic_text="vehicles", signal_type=SignalType.ENGAGED_MENTION,
        valence=InterestValence.POSITIVE, source_id="other:hmac-only",
        now=conflict.created_at, event_id=conflict.event_id,
    )
    with pytest.raises(ValueError, match="conflicts with stored evidence"):
        store.import_reviewed_interest_seed(
            events, run_id=f"reviewed-link-interest-seed:{manifest['review_id']}",
            source_hash=manifest["review_id"], manifest={"review_id": manifest["review_id"]},
            now=max(event.created_at for event in events),
        )
    assert store.list_interests() == []
    assert store.unfolded_interest_events() == [store.unfolded_interest_events()[0]]
    assert store.unfolded_interest_events()[0].topic_text == "vehicles"
