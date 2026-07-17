from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

from gateway.contact_memory.admin import main as admin_main
from gateway.contact_memory.broker import ContactMemoryBroker, RetrievalScope
from gateway.contact_memory.extractor import validate_pending_operation
from gateway.contact_memory.gating import TurnState
from gateway.contact_memory.import_contacts import import_jsonl
from gateway.contact_memory.registry import EntityRegistry
from gateway.contact_memory.schema import (
    AssertionType, Audience, FactProposal, FactStatus, MentionPolicy, RetrievalPrincipal,
)
from gateway.contact_memory.store import ContactMemoryStore


def proposal(contact: str, logical: str, text: str, **kw) -> FactProposal:
    values = dict(
        logical_id=logical, subject_id="person:contact", predicate="has_preference",
        object_text=text, audience=Audience.GUEST_OK,
        mention_policy=MentionPolicy.MENTIONABLE, assertion_type=AssertionType.STATED,
        source_id=f"source:{logical}", source_contact_id=contact,
        evidence_pointer="synthetic/1", trust=.95, confidence=.95,
    )
    values.update(kw)
    return FactProposal(**values)


def test_broker_is_physically_scoped_and_namespace_cannot_be_in_query(tmp_path: Path):
    a = ContactMemoryStore(tmp_path, "contact-a")
    b = ContactMemoryStore(tmp_path, "contact-b")
    fact_a = a.supersede_fact(proposal("contact-a", "trip", "The planned trip is to Lisbon."))
    b.supersede_fact(proposal("contact-b", "trip", "The planned trip is to Oslo."))
    broker = ContactMemoryBroker(tmp_path)
    scope = RetrievalScope(RetrievalPrincipal.GUEST, "contact-a", "session-a")

    bundle = broker.search(scope, "trip Lisbon contact-b ../contacts", turn_index=1)

    assert bundle.fact_ids == (fact_a.version_id,)
    assert "Lisbon" in bundle.rendered
    assert "Oslo" not in bundle.rendered


def test_pending_inferred_restricted_and_revoked_are_never_guest_candidates(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact-a")
    visible = store.supersede_fact(proposal("contact-a", "visible", "Favorite snack is popcorn."))
    store.supersede_fact(proposal("contact-a", "pending", "Pending popcorn detail.", status=FactStatus.PENDING))
    store.supersede_fact(proposal("contact-a", "inferred", "Inferred popcorn detail.", assertion_type=AssertionType.INFERRED))
    store.supersede_fact(proposal("contact-a", "restricted", "Restricted popcorn detail.", mention_policy=MentionPolicy.RESTRICTED))
    assert [row.fact.version_id for row in store.lexical_search(RetrievalPrincipal.GUEST, "popcorn", limit=20)] == [visible.version_id]
    assert store.revoke_fact("visible")
    assert store.lexical_search(RetrievalPrincipal.GUEST, "popcorn") == []


def test_unauthorized_and_absent_are_indistinguishable_to_guest(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact-a")
    store.supersede_fact(proposal("contact-a", "secret", "The vault word is synthetic.", audience=Audience.OWNER_ONLY))
    broker = ContactMemoryBroker(tmp_path)
    scope = RetrievalScope(RetrievalPrincipal.GUEST, "contact-a", "session")
    unauthorized = broker.search(scope, "vault synthetic")
    absent = broker.search(scope, "not-present-token")
    assert unauthorized == absent


def test_lane_a_gate_cooldown_and_usage_penalty(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact-a")
    store.supersede_fact(proposal("contact-a", "car", "The car under discussion is the green hatchback."))
    broker = ContactMemoryBroker(tmp_path)
    scope = RetrievalScope(RetrievalPrincipal.GUEST, "contact-a", "session")
    first = broker.prefetch(scope, "should i keep the car", [], TurnState(register="advice", turn_index=1, now=1000))
    second = broker.prefetch(scope, "should i sell the car", [], TurnState(register="advice", turn_index=2, now=1200))
    third = broker.prefetch(scope, "should i sell the car", [], TurnState(register="advice", turn_index=6, now=1200))
    assert first.fact_ids
    assert second.empty  # five-message cooldown still active
    assert third.empty  # recent callback receives a hard ranking penalty


def test_recommendations_supersede_atomically_and_require_basis(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact-a")
    fact = store.supersede_fact(proposal("contact-a", "car", "The car is costly."))
    first = store.set_recommendation("car", "keep", [fact.version_id], confidence=.8)
    second = store.set_recommendation("car", "sell", [fact.version_id], confidence=.9)
    active = store.active_recommendations()
    assert len(active) == 1 and active[0]["recommendation_id"] == second
    assert active[0]["supersedes_id"] == first


def test_extractor_output_is_forced_pending_and_cannot_choose_namespace():
    raw = {
        "logical_id": "x", "subject_id": "person:contact", "predicate": "likes",
        "object_text": "Synthetic fact", "audience": "guest_ok", "assertion_type": "stated",
    }
    parsed = validate_pending_operation(raw, "trusted-contact", "trusted-source")
    assert parsed.status is FactStatus.PENDING
    assert parsed.source_contact_id == "trusted-contact"
    hostile = dict(raw, source_contact_id="other")
    try:
        validate_pending_operation(hostile, "trusted-contact", "trusted-source")
    except ValueError:
        pass
    else:
        raise AssertionError("extractor namespace injection was accepted")


def test_importer_dry_run_accounts_for_every_synthetic_row_and_writes_nothing(tmp_path: Path):
    dossier = tmp_path / "synthetic.jsonl"
    rows = [
        {"id": "1", "subject": "person:contact", "fact": "Likes tea", "confidence": .9, "trust": .9},
        {"id": "2", "subject": "third_party", "fact": "Third party detail", "confidence": .9},
        {"id": "3", "subject": "person:contact", "fact": "Sensitive detail", "sensitive": True},
        {"id": "4", "subject": "person:contact", "fact": "Reviewed fact", "audience": "guest_ok", "guest_reviewed": True, "confidence": .9, "trust": .9},
    ]
    dossier.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = import_jsonl(dossier, "contact-a", root=tmp_path / "memory", dry_run=True)
    assert result["counts"]["rows"] == 4
    assert sum(result["counts"].get(key, 0) for key in ("owner_only", "quarantined", "guest_ok", "rejected")) == 4
    assert not (tmp_path / "memory").exists()


def test_importer_accepts_existing_contact_persona_schema_conservatively(tmp_path: Path):
    dossier = tmp_path / "contact-persona.jsonl"
    dossier.write_text(json.dumps({
        "person_id": "stephen-lucier",
        "fact_id": "stephen_identity_001",
        "fact_type": "identity",
        "fact_value": "Synthetic identity detail",
        "source_record_id": "message-123",
        "confidence": "high",
        "sensitivity": "normal",
    }) + "\n", encoding="utf-8")

    result = import_jsonl(dossier, "stephen-lucier", root=tmp_path / "memory", dry_run=True)

    assert result["errors"] == []
    assert result["source_ids"] == ["stephen_identity_001"]
    # The source schema has no normalized subject/audience review, so it must
    # never become Guest-visible merely because it came from Steve's thread.
    assert result["counts"] == {"rows": 1, "quarantined": 1}
    assert not (tmp_path / "memory").exists()


def test_explicit_guest_review_allows_sensitive_guest_or_relationship_fact(tmp_path: Path):
    dossier = tmp_path / "reviewed-sensitive.jsonl"
    rows = [
        {
            "id": "guest-sensitive", "subject": "person:contact",
            "fact": "Reviewed private fact about the guest", "sensitive": True,
            "audience": "guest_ok", "guest_reviewed": True,
            "confidence": .9, "trust": .9,
        },
        {
            "id": "shared-sensitive", "subject": "relationship:owner-contact",
            "fact": "Reviewed private fact about the relationship", "sensitive": True,
            "audience": "guest_ok", "guest_reviewed": True,
            "confidence": .9, "trust": .9,
        },
    ]
    dossier.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = import_jsonl(dossier, "contact-a", root=tmp_path / "memory", dry_run=False)

    assert result["counts"]["guest_ok"] == 2
    facts = ContactMemoryStore(tmp_path / "memory", "contact-a").active_facts(RetrievalPrincipal.GUEST)
    assert {fact.source_id for fact in facts} == {"guest-sensitive", "shared-sensitive"}
    assert all(fact.mention_policy is MentionPolicy.MENTIONABLE for fact in facts)


def test_ambiguous_fact_requires_explicit_guest_review(tmp_path: Path):
    dossier = tmp_path / "ambiguous.jsonl"
    rows = [
        {
            "id": "ambiguous-unreviewed", "subject": "ambiguous",
            "fact": "Unreviewed third-party detail", "sensitive": True,
            "confidence": .9, "trust": .9,
        },
        {
            "id": "ambiguous-reviewed", "subject": "ambiguous",
            "fact": "Reviewed shared detail involving a third party", "sensitive": True,
            "audience": "guest_ok", "guest_reviewed": True,
            "confidence": .9, "trust": .9,
        },
    ]
    dossier.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = import_jsonl(dossier, "contact-a", root=tmp_path / "memory", dry_run=True)

    assert result["counts"]["quarantined"] == 1
    assert result["counts"]["guest_ok"] == 1


def test_import_is_idempotent_and_quarantine_never_supersedes_reviewed_active(tmp_path: Path):
    dossier = tmp_path / "dossier.jsonl"
    dossier.write_text(json.dumps({
        "id": "reviewed-source", "logical_id": "same-logical",
        "subject": "person:contact", "fact": "Reviewed visible value",
        "audience": "guest_ok", "guest_reviewed": True,
        "confidence": .9, "trust": .9,
    }) + "\n", encoding="utf-8")
    root = tmp_path / "memory"
    first = import_jsonl(dossier, "contact-a", root=root, dry_run=False)
    retry = import_jsonl(dossier, "contact-a", root=root, dry_run=False)
    assert first["counts"]["imported"] == 1
    assert retry["counts"]["imported"] == 0
    assert retry["counts"]["skipped_existing"] == 1

    quarantined = proposal(
        "contact-a", "same-logical", "Unreviewed retry value",
        source_id="new-quarantined-source", status=FactStatus.QUARANTINED,
        audience=Audience.OWNER_ONLY,
    )
    store = ContactMemoryStore(root, "contact-a")
    store.import_proposals([quarantined])
    active = store.active_facts(RetrievalPrincipal.GUEST)
    assert len(active) == 1 and active[0].object_text == "Reviewed visible value"
    assert store.count_versions("same-logical") == 2


def test_import_rolls_back_entire_dossier_on_mid_batch_failure(tmp_path: Path, monkeypatch):
    store = ContactMemoryStore(tmp_path, "contact-a")
    proposals = [
        proposal("contact-a", "one", "First", source_id="source-one"),
        proposal("contact-a", "two", "Second", source_id="source-two"),
    ]
    original = ContactMemoryStore._insert_fact
    calls = 0

    def fail_second(con, item, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("synthetic failure")
        return original(con, item, **kwargs)

    monkeypatch.setattr(ContactMemoryStore, "_insert_fact", staticmethod(fail_second))
    try:
        store.import_proposals(proposals)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("synthetic failure was swallowed")
    assert store.count_versions("one") == 0
    assert store.count_versions("two") == 0


def test_apply_with_invalid_row_writes_nothing(tmp_path: Path):
    dossier = tmp_path / "invalid.jsonl"
    dossier.write_text(
        json.dumps({
            "id": "valid", "subject": "person:contact", "fact": "Valid prefix",
            "confidence": .9, "trust": .9,
        }) + "\n{not-json}\n",
        encoding="utf-8",
    )
    root = tmp_path / "memory"
    result = import_jsonl(dossier, "contact-a", root=root, dry_run=False)
    assert result["counts"]["imported"] == 0
    assert result["errors"]
    assert not root.exists()


def test_gate_state_is_lru_and_ttl_bounded():
    from gateway.contact_memory.gating import LaneAGate

    gate = LaneAGate(max_sessions=2, session_ttl_seconds=10)
    turn = lambda now: TurnState(register="casual", turn_index=0, now=now)
    gate.evaluate("a", "hello there", [], turn(1))
    gate.evaluate("b", "hello there", [], turn(2))
    gate.evaluate("c", "hello there", [], turn(3))
    assert list(gate._sessions) == ["b", "c"]
    gate.evaluate("d", "hello there", [], turn(20))
    assert list(gate._sessions) == ["d"]

    started = time.perf_counter()
    for index in range(100):
        gate.evaluate("d", "hello there", [], turn(21 + index))
    per_turn_ms = (time.perf_counter() - started) * 1000 / 100
    assert per_turn_ms < 120, f"gate overhead was {per_turn_ms:.1f} ms/turn"


def test_admin_accept_reject_export_delete_workflow(tmp_path: Path, capsys):
    root = tmp_path / "memory"
    store = ContactMemoryStore(root, "contact-a")
    accepted_id = store.add_pending(
        proposal("contact-a", "accepted", "Accepted synthetic value", status=FactStatus.PENDING),
        "pending-accept",
    )
    rejected_id = store.add_pending(
        proposal("contact-a", "rejected", "Rejected synthetic value", status=FactStatus.PENDING),
        "pending-reject",
    )
    base = ["--contact-id", "contact-a", "--root", str(root)]
    assert admin_main([*base, "accept", accepted_id, "--audience", "guest_ok", "--mention-policy", "mentionable"]) == 0
    capsys.readouterr()
    assert admin_main([*base, "reject", rejected_id]) == 0
    capsys.readouterr()
    output = tmp_path / "export.json"
    assert admin_main([*base, "export", "--output", str(output)]) == 0
    capsys.readouterr()
    payload = json.loads(output.read_text())
    assert [fact["object_text"] for fact in payload["facts"]] == ["Accepted synthetic value"]
    assert admin_main([*base, "delete", "--confirm", "contact-a"]) == 0
    capsys.readouterr()
    assert ContactMemoryStore(root, "contact-a").active_facts(RetrievalPrincipal.OWNER) == []


def test_global_registry_stores_entities_but_no_fact_text(tmp_path: Path):
    import sqlite3

    registry = EntityRegistry(tmp_path)
    registry.upsert("person:contact", "person", "Synthetic Contact", ["Contact"])
    entity = registry.get("person:contact")
    assert entity and entity["aliases"] == ["Contact"]
    with sqlite3.connect(registry.path) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "entity" in tables
    assert "fact" not in tables
