from __future__ import annotations

import concurrent.futures
import hashlib
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from gateway.contact_memory.schema import (
    SCHEMA_VERSION,
    CommunicationActorRole,
    CommunicationAttachment,
    CommunicationDirection,
    CommunicationEnrichmentState,
    CommunicationEvent,
    CommunicationKind,
    CommunicationLifecycle,
    CommunicationPrivacy,
    CommunicationReactionSubtype,
    CommunicationRecommendationEvent,
    CommunicationRecommendationOutcome,
    CommunicationRelation,
    CommunicationRelationType,
    CommunicationUrl,
    EntityMention,
)
from gateway.contact_memory.store import ContactMemoryStore


def _id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _event(name: str = "message-1", **overrides: object) -> CommunicationEvent:
    values: dict[str, object] = {
        "event_id": _id(f"event:{name}"),
        "platform": "bluebubbles",
        "source_id": _id(f"source:{name}"),
        "occurred_at": 100.0,
        "direction": CommunicationDirection.INBOUND,
        "kind": CommunicationKind.LINK_SHARE,
        "actor_role": CommunicationActorRole.CONTACT,
        "privacy": CommunicationPrivacy.PRIVATE,
        "text_hash": _id(f"text:{name}"),
        "text_present": True,
        "text_length": 18,
        "provenance": "synthetic-fixture",
    }
    values.update(overrides)
    return CommunicationEvent(**values)  # type: ignore[arg-type]


def _children(event: CommunicationEvent) -> dict[str, Any]:
    return {
        "urls": (CommunicationUrl(
            url_id=_id("url-row"), event_id=event.event_id,
            url_identity=_id("canonical-url"), domain="example.com",
            sharer_role=CommunicationActorRole.CONTACT,
            enrichment_state=CommunicationEnrichmentState.REVIEWED,
            platform="web",
        ),),
        "attachments": (CommunicationAttachment(
            attachment_id=_id("attachment-row"), event_id=event.event_id,
            attachment_identity=_id("attachment-source"), media_kind="image",
            mime_type="image/jpeg", uti="public.jpeg", size_bytes=2048,
            caption_present=True, caption_hash=_id("caption"),
        ),),
        "relations": (CommunicationRelation(
            relation_id=_id("relation-row"), event_id=event.event_id,
            relation_type=CommunicationRelationType.REPLY_TO,
            target_source_id=_id("target-source"),
            target_actor_role=CommunicationActorRole.COUNTERPART,
        ),),
        "entity_mentions": (EntityMention(
            mention_id=_id("mention-row"), event_id=event.event_id,
            entity_identity=_id("entity"), entity_type="thing",
            canonical_label="synthetic album", confidence=0.9,
            source_method="reviewed", surface_hash=_id("surface"),
        ),),
        "recommendation_events": (CommunicationRecommendationEvent(
            recommendation_event_id=_id("recommendation-row"),
            recommendation_id="a" * 32, event_id=event.event_id,
            outcome=CommunicationRecommendationOutcome.PROPOSED,
            confidence=1.0, explicit_linkage=True,
        ),),
    }


def test_v4_migration_is_additive_and_idempotent(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    with sqlite3.connect(store.path) as con:
        con.executescript("""
        DROP TABLE communication_recommendation_event;
        DROP TABLE entity_mention;
        DROP TABLE communication_retraction_pending;
        DROP TABLE communication_relation;
        DROP TABLE communication_attachment;
        DROP TABLE communication_url;
        DROP TABLE communication_event;
        """)
        con.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
        con.execute("INSERT INTO interest_event VALUES(?,?,?,?,?,?,NULL)", (
            "legacy-event", "music", "enthusiasm", "positive", "legacy-source", 1.0,
        ))

    reopened = ContactMemoryStore(tmp_path, "contact")
    ContactMemoryStore(tmp_path, "contact")
    with sqlite3.connect(reopened.path) as con:
        assert con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        assert con.execute("SELECT topic_text FROM interest_event WHERE event_id='legacy-event'").fetchone()[0] == "music"
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables.issuperset({
        "communication_event", "communication_url", "communication_attachment",
        "communication_relation", "entity_mention", "communication_recommendation_event",
    })


def test_v5_migration_preserves_untyped_reactions_as_legacy_history(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    with sqlite3.connect(store.path) as con:
        con.executescript("""
        DROP TABLE communication_recommendation_event;
        DROP TABLE entity_mention;
        DROP TABLE communication_retraction_pending;
        DROP TABLE communication_relation;
        DROP TABLE communication_attachment;
        DROP TABLE communication_url;
        DROP TABLE communication_event;
        CREATE TABLE communication_event (
          event_id TEXT PRIMARY KEY, platform TEXT NOT NULL, source_id TEXT NOT NULL,
          occurred_at REAL NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL,
          actor_role TEXT NOT NULL, privacy TEXT NOT NULL, lifecycle TEXT NOT NULL,
          text_hash TEXT, text_present INTEGER NOT NULL, text_length INTEGER NOT NULL,
          provenance TEXT NOT NULL, provenance_version INTEGER NOT NULL,
          retracted_by_event_id TEXT, UNIQUE(platform, source_id)
        );
        """)
        legacy = _event("legacy-v5")
        reaction_event_id = _id("event:legacy-v5-reaction")
        reaction_source_id = _id("source:legacy-v5-reaction")
        con.execute(
            "INSERT INTO communication_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                legacy.event_id, legacy.platform, legacy.source_id, legacy.occurred_at,
                legacy.direction.value, legacy.kind.value, legacy.actor_role.value,
                legacy.privacy.value, legacy.lifecycle.value, legacy.text_hash,
                int(legacy.text_present), legacy.text_length, legacy.provenance,
                legacy.provenance_version, legacy.retracted_by_event_id,
            ),
        )
        con.execute(
            "INSERT INTO communication_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reaction_event_id, "bluebubbles", reaction_source_id, 101.0,
                CommunicationDirection.INBOUND.value, CommunicationKind.REACTION_ADD.value,
                CommunicationActorRole.CONTACT.value, CommunicationPrivacy.PRIVATE.value,
                CommunicationLifecycle.ACTIVE.value, None, 0, 0, "legacy-migration", 1, None,
            ),
        )
        con.execute("UPDATE schema_meta SET value='5' WHERE key='schema_version'")

    reopened = ContactMemoryStore(tmp_path, "contact")
    ContactMemoryStore(tmp_path, "contact")
    with sqlite3.connect(reopened.path) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(communication_event)")}
        version = con.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        stored_subtype = con.execute(
            "SELECT reaction_subtype FROM communication_event WHERE event_id=?",
            (reaction_event_id,),
        ).fetchone()[0]
    assert "reaction_subtype" in columns
    assert version == str(SCHEMA_VERSION)
    assert stored_subtype == CommunicationReactionSubtype.LEGACY_UNTYPED.value
    assert reopened.get_communication_event(legacy.event_id) == legacy
    migrated_reaction = reopened.get_communication_event(reaction_event_id)
    assert migrated_reaction is not None
    assert migrated_reaction.reaction_subtype is CommunicationReactionSubtype.LEGACY_UNTYPED


def test_bundle_ingest_round_trips_every_child_and_exact_replay_is_idempotent(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    event = _event()
    first = store.ingest_communication_event(event, **_children(event))
    replay = store.ingest_communication_event(event, **_children(event))

    assert first.inserted and not first.deduplicated
    assert replay.deduplicated and not replay.inserted
    bundle = store.get_communication_bundle(event.event_id)
    assert bundle is not None and bundle.event == event
    assert len(bundle.urls) == len(bundle.attachments) == len(bundle.relations) == 1
    assert len(bundle.entity_mentions) == len(bundle.recommendation_events) == 1
    assert store.get_communication_event_by_source("bluebubbles", event.source_id) == event


def test_child_enrichment_is_additive_idempotent_and_supports_review_transition(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    event = _event("enrichment")
    pending = CommunicationUrl(
        url_id=_id("enrichment-url"), event_id=event.event_id,
        url_identity=_id("enrichment-identity"), domain="example.com",
        sharer_role=CommunicationActorRole.CONTACT,
    )
    store.ingest_communication_event(event, urls=(pending,))
    mention = EntityMention(
        mention_id=_id("late-mention"), event_id=event.event_id,
        entity_identity=_id("late-entity"), entity_type="thing",
        canonical_label="synthetic artist", confidence=0.8, source_method="reviewed",
    )
    reviewed = CommunicationUrl(
        **{**pending.__dict__, "enrichment_state": CommunicationEnrichmentState.REVIEWED}
    )
    first = store.enrich_communication_event(
        event.event_id, urls=(reviewed,), entity_mentions=(mention,)
    )
    replay = store.enrich_communication_event(
        event.event_id, urls=(reviewed,), entity_mentions=(mention,)
    )
    ingress_replay = store.ingest_communication_event(event, urls=(pending,))
    assert first == replay
    assert ingress_replay.deduplicated
    assert first.urls == (reviewed,)
    assert first.entity_mentions == (mention,)


def test_conflicting_source_or_child_rolls_back_entire_bundle(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    first = _event("first")
    store.ingest_communication_event(first, **_children(first))

    conflicting_source = _event("second", source_id=first.source_id)
    with pytest.raises(ValueError, match="source"):
        store.ingest_communication_event(conflicting_source, **_children(conflicting_source))
    assert store.get_communication_event(conflicting_source.event_id) is None

    child_conflict = CommunicationUrl(
        url_id=_id("url-row"), event_id=_id("event:third"),
        url_identity=_id("different-url"), domain="example.org",
        sharer_role=CommunicationActorRole.CONTACT,
    )
    third = _event("third")
    with pytest.raises(ValueError, match="child|url"):
        store.ingest_communication_event(third, urls=(child_conflict,))
    assert store.get_communication_event(third.event_id) is None


def test_concurrent_replay_inserts_exactly_one_bundle(tmp_path: Path):
    ContactMemoryStore(tmp_path, "contact")
    event = _event("concurrent")

    def ingest(_: int):
        return ContactMemoryStore(tmp_path, "contact").ingest_communication_event(event)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(ingest, range(12)))
    assert sum(result.inserted for result in results) == 1
    assert sum(result.deduplicated for result in results) == 11


def test_reaction_removal_retracts_target_atomically_and_replays(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    target = _event("reaction-add", kind=CommunicationKind.REACTION_ADD,
                    reaction_subtype=CommunicationReactionSubtype.LIKE,
                    text_hash=None, text_present=False, text_length=0)
    original_source = _id("reacted-to-message")
    target_relation = CommunicationRelation(
        relation_id=_id("add-target"), event_id=target.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=original_source,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    store.ingest_communication_event(target, relations=(target_relation,))
    removal = _event("reaction-remove", kind=CommunicationKind.REACTION_REMOVE,
                     reaction_subtype=CommunicationReactionSubtype.LIKE,
                     text_hash=None, text_present=False, text_length=0)
    relation = CommunicationRelation(
        relation_id=_id("remove-target"), event_id=removal.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=original_source,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    )

    first = store.retract_communication_event(removal, target_event_id=target.event_id,
                                              relations=(relation,))
    replay = store.retract_communication_event(removal, target_event_id=target.event_id,
                                               relations=(relation,))
    assert first.inserted and replay.deduplicated
    retracted = store.get_communication_event(target.event_id)
    assert retracted is not None
    assert retracted.lifecycle is CommunicationLifecycle.RETRACTED
    assert retracted.retracted_by_event_id == removal.event_id
    original_replay = store.ingest_communication_event(target, relations=(target_relation,))
    assert original_replay.deduplicated
    assert original_replay.event.lifecycle is CommunicationLifecycle.RETRACTED

    other = _event("other-remove", kind=CommunicationKind.REACTION_REMOVE,
                   reaction_subtype=CommunicationReactionSubtype.LIKE,
                   text_hash=None, text_present=False, text_length=0)
    with pytest.raises(ValueError, match="retracted"):
        store.retract_communication_event(other, target_event_id=target.event_id,
            relations=(CommunicationRelation(
                relation_id=_id("other-remove-target"), event_id=other.event_id,
                relation_type=CommunicationRelationType.REACTION_TO,
                target_source_id=original_source,
                target_actor_role=CommunicationActorRole.COUNTERPART,
            ),))
    assert store.get_communication_event(other.event_id) is None


def test_out_of_order_retraction_is_pending_and_applies_when_target_arrives(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    removal = _event("orphan-remove", kind=CommunicationKind.REACTION_REMOVE,
                     reaction_subtype=CommunicationReactionSubtype.LOVE,
                     text_hash=None, text_present=False, text_length=0)
    target = _event("late-add", kind=CommunicationKind.REACTION_ADD,
                    reaction_subtype=CommunicationReactionSubtype.LOVE,
                    text_hash=None, text_present=False, text_length=0)
    original_source = _id("late-original")
    removal_relation = CommunicationRelation(
        relation_id=_id("late-remove-relation"), event_id=removal.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=original_source,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    pending = store.retract_communication_event(
        removal, target_event_id=target.event_id, relations=(removal_relation,)
    )
    assert pending.inserted
    target_relation = CommunicationRelation(
        relation_id=_id("late-add-relation"), event_id=target.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=original_source,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    store.ingest_communication_event(target, relations=(target_relation,))
    stored = store.get_communication_event(target.event_id)
    assert stored is not None and stored.lifecycle is CommunicationLifecycle.RETRACTED
    assert stored.retracted_by_event_id == removal.event_id


def test_reaction_removal_cannot_bypass_or_retract_multiple_targets(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    removal = _event("guarded-remove", kind=CommunicationKind.REACTION_REMOVE,
                     reaction_subtype=CommunicationReactionSubtype.LAUGH,
                     text_hash=None, text_present=False, text_length=0)
    with pytest.raises(ValueError, match="retract_communication_event"):
        store.ingest_communication_event(removal)
    relationless_add = _event(
        "relationless-add", kind=CommunicationKind.REACTION_ADD,
        reaction_subtype=CommunicationReactionSubtype.LAUGH,
        text_hash=None, text_present=False, text_length=0,
    )
    with pytest.raises(ValueError, match="exactly one"):
        store.ingest_communication_event(relationless_add)
    relations = tuple(CommunicationRelation(
        relation_id=_id(f"multi-{index}"), event_id=removal.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=_id(f"original-{index}"),
    ) for index in range(2))
    with pytest.raises(ValueError, match="exactly one"):
        store.retract_communication_event(
            removal, target_event_id=_id("target"), relations=relations
        )


def test_reaction_removal_requires_exact_subtype_match(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    target = _event(
        "loved-add", kind=CommunicationKind.REACTION_ADD,
        reaction_subtype=CommunicationReactionSubtype.LOVE,
        text_hash=None, text_present=False, text_length=0,
    )
    target_source = _id("same-reacted-to-message")
    target_relation = CommunicationRelation(
        relation_id=_id("loved-add-relation"), event_id=target.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    store.ingest_communication_event(target, relations=(target_relation,))
    removal = _event(
        "liked-removal", kind=CommunicationKind.REACTION_REMOVE,
        reaction_subtype=CommunicationReactionSubtype.LIKE,
        text_hash=None, text_present=False, text_length=0,
    )
    removal_relation = CommunicationRelation(
        relation_id=_id("liked-removal-relation"), event_id=removal.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    )

    with pytest.raises(ValueError, match="subtype"):
        store.retract_communication_event(
            removal, target_event_id=target.event_id, relations=(removal_relation,)
        )
    assert store.get_communication_event(removal.event_id) is None
    stored = store.get_communication_event(target.event_id)
    assert stored is not None and stored.lifecycle is CommunicationLifecycle.ACTIVE


def test_out_of_order_reaction_removal_rejects_mismatched_subtype_atomically(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    target = _event(
        "late-loved-add", kind=CommunicationKind.REACTION_ADD,
        reaction_subtype=CommunicationReactionSubtype.LOVE,
        text_hash=None, text_present=False, text_length=0,
    )
    removal = _event(
        "early-liked-remove", kind=CommunicationKind.REACTION_REMOVE,
        reaction_subtype=CommunicationReactionSubtype.LIKE,
        text_hash=None, text_present=False, text_length=0,
    )
    target_source = _id("late-same-reacted-to-message")
    removal_relation = CommunicationRelation(
        relation_id=_id("early-liked-remove-relation"), event_id=removal.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    store.retract_communication_event(
        removal, target_event_id=target.event_id, relations=(removal_relation,)
    )
    target_relation = CommunicationRelation(
        relation_id=_id("late-loved-add-relation"), event_id=target.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    )

    with pytest.raises(ValueError, match="subtype"):
        store.ingest_communication_event(target, relations=(target_relation,))
    assert store.get_communication_event(target.event_id) is None
    assert store.get_communication_event(removal.event_id) == removal


def test_reaction_subtype_is_required_only_for_reaction_events():
    with pytest.raises(ValueError, match="reaction_subtype"):
        _event("untyped-reaction", kind=CommunicationKind.REACTION_ADD)
    with pytest.raises(ValueError, match="reaction_subtype"):
        _event("typed-text", reaction_subtype=CommunicationReactionSubtype.LIKE)


@pytest.mark.parametrize("kind", (
    CommunicationKind.REACTION_ADD,
    CommunicationKind.REACTION_REMOVE,
))
def test_legacy_untyped_reaction_subtype_is_not_valid_for_new_events(
    tmp_path: Path, kind: CommunicationKind
):
    with pytest.raises(ValueError, match="historical"):
        _event(
            f"new-legacy-{kind.value}", kind=kind,
            reaction_subtype=CommunicationReactionSubtype.LEGACY_UNTYPED,
            text_hash=None, text_present=False, text_length=0,
        )

    store = ContactMemoryStore(tmp_path, "contact")
    event = _event(
        f"mutated-legacy-{kind.value}", kind=kind,
        reaction_subtype=CommunicationReactionSubtype.LIKE,
        text_hash=None, text_present=False, text_length=0,
    )
    object.__setattr__(event, "reaction_subtype", CommunicationReactionSubtype.LEGACY_UNTYPED)
    with pytest.raises(ValueError, match="historical"):
        if kind is CommunicationKind.REACTION_ADD:
            store.ingest_communication_event(event)
        else:
            store.retract_communication_event(
                event, target_event_id=_id("legacy-target"), relations=()
            )


def test_occurred_at_normalizes_exactly_and_replays_at_float_boundary(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    for index, timestamp in enumerate((2**53, 2**53 + 2, -(2**53))):
        event = _event(f"exact-timestamp-{index}", occurred_at=timestamp)
        assert isinstance(event.occurred_at, float)
        assert event.occurred_at == timestamp
        store.ingest_communication_event(event)
        assert store.get_communication_event(event.event_id) == event
        assert store.ingest_communication_event(event).deduplicated


def test_occurred_at_rejects_integer_that_float_cannot_represent_exactly():
    with pytest.raises(ValueError, match="exactly"):
        _event("lossy-timestamp", occurred_at=2**53 + 1)


def test_contract_rejects_raw_artifact_shapes_and_noncanonical_ids():
    with pytest.raises(ValueError):
        _event(event_id="A" * 64)
    event = _event()
    with pytest.raises(ValueError, match="domain"):
        CommunicationUrl(
            url_id=_id("raw-url"), event_id=event.event_id,
            url_identity=_id("url"), domain="https://example.com/path?token=secret",
            sharer_role=CommunicationActorRole.CONTACT,
        )
    with pytest.raises(ValueError):
        CommunicationAttachment(
            attachment_id=_id("raw-path"), event_id=event.event_id,
            attachment_identity=_id("attachment"), media_kind="image",
            mime_type="image/jpeg\n/private/path",
        )
    for field, value in (("mime_type", "/private/secret"),
                         ("mime_type", "image/jpeg;token=secret"),
                         ("uti", "https://host/path?token=secret")):
        invalid_field: dict[str, Any] = {field: value}
        with pytest.raises(ValueError):
            CommunicationAttachment(
                attachment_id=_id(f"raw-{field}-{value}"), event_id=event.event_id,
                attachment_identity=_id(f"identity-{field}-{value}"),
                media_kind="image", **invalid_field,
            )
    with pytest.raises(ValueError):
        CommunicationUrl(
            url_id=_id("raw-platform"), event_id=event.event_id,
            url_identity=_id("raw-platform-url"), domain="example.com",
            sharer_role=CommunicationActorRole.CONTACT,
            platform="https://host/path?token=secret",
        )
    with pytest.raises(ValueError, match="integer"):
        _event(text_length=1.5)
    with pytest.raises(ValueError, match="integer"):
        _event(provenance_version=1.5)
    with pytest.raises(ValueError, match="local path"):
        EntityMention(
            mention_id=_id("raw-entity"), event_id=event.event_id,
            entity_identity=_id("raw-entity-identity"), entity_type="thing",
            canonical_label="see /etc/passwd", confidence=1.0,
            source_method="reviewed",
        )
    with pytest.raises(ValueError, match="local path"):
        EntityMention(
            mention_id=_id("relative-path-entity"), event_id=event.event_id,
            entity_identity=_id("relative-path-identity"), entity_type="thing",
            canonical_label="Library/Messages/Attachments/private.jpg", confidence=1.0,
            source_method="reviewed",
        )
    for label in ("etc/passwd", "Documents/Payroll", "AC/DC"):
        with pytest.raises(ValueError, match="local path"):
            EntityMention(
                mention_id=_id(f"slash-label-{label}"), event_id=event.event_id,
                entity_identity=_id(f"slash-identity-{label}"), entity_type="thing",
                canonical_label=label, confidence=1.0, source_method="reviewed",
            )
    for label in (
        "password hunter2 api secret",
        "my password is hunter2",
        "client secret abc123",
        "bearer token eyJhbG...NiJ9",
        "GitHub token is ghp_123456789",
        "AWS secret access key is abc123",
        "database credential hunter2",
    ):
        with pytest.raises(ValueError, match="credential-like"):
            EntityMention(
                mention_id=_id(f"secret-label-{label}"), event_id=event.event_id,
                entity_identity=_id(f"secret-identity-{label}"), entity_type="thing",
                canonical_label=label, confidence=1.0, source_method="reviewed",
            )


@pytest.mark.parametrize("label", (
    "secret access key is abc123",
    "my credential is hunter2",
    "credentials are hunter2",
    "AWS's secret access key is abc123",
))
def test_entity_semantic_labels_reject_generic_credential_values(label: str):
    event = _event(f"credential-label-{label}")
    with pytest.raises(ValueError, match="credential-like"):
        EntityMention(
            mention_id=_id(f"credential-label-{label}"), event_id=event.event_id,
            entity_identity=_id(f"credential-identity-{label}"), entity_type="thing",
            canonical_label=label, confidence=1.0, source_method="reviewed",
        )


@pytest.mark.parametrize("label", (
    "Secret Garden",
    "Password",
    "The Great British Bake Off",
    "The Place Beyond the Pines",
    "Florence + the Machine",
    "API Gallery",
    "GitHub",
    "AWS",
    "Database",
    "My Chemical Romance",
    "The Secret Garden",
    "Credential Coffee",
    "Tokens and Secrets",
))
def test_entity_semantic_labels_allow_legitimate_names(label: str):
    event = _event(f"legitimate-label-{label}")
    mention = EntityMention(
        mention_id=_id(f"legitimate-label-{label}"), event_id=event.event_id,
        entity_identity=_id(f"legitimate-identity-{label}"), entity_type="thing",
        canonical_label=label, confidence=1.0, source_method="reviewed",
    )
    assert mention.canonical_label == label


def test_pending_retraction_target_identity_must_be_opaque(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    removal = _event("raw-target-remove", kind=CommunicationKind.REACTION_REMOVE,
                     reaction_subtype=CommunicationReactionSubtype.QUESTION,
                     text_hash=None, text_present=False, text_length=0)
    relation = CommunicationRelation(
        relation_id=_id("raw-target-relation"), event_id=removal.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=_id("original-target"),
    )
    with pytest.raises(ValueError, match="opaque"):
        store.retract_communication_event(
            removal, target_event_id="https://host/path?token=secret", relations=(relation,)
        )
    assert store.get_communication_event(removal.event_id) is None


def test_communication_events_are_physically_isolated_and_secure_delete_clears_them(tmp_path: Path):
    first = ContactMemoryStore(tmp_path, "contact-a")
    second = ContactMemoryStore(tmp_path, "contact-b")
    event = _event("isolated")
    first.ingest_communication_event(event)
    assert second.get_communication_event(event.event_id) is None
    first.secure_delete_all()
    assert first.get_communication_event(event.event_id) is None
