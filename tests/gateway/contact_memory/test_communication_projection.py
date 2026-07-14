from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

from gateway.contact_memory.schema import (
    CallbackProjection,
    CommunicationActorRole,
    CommunicationDirection,
    CommunicationEvent,
    CommunicationKind,
    CommunicationLifecycle,
    CommunicationProjection,
    CommunicationReactionSubtype,
    CommunicationRelation,
    CommunicationRelationType,
    EntityProjection,
    InterestProjection,
    InterestValence,
    ProjectionMethod,
    RecommendationProjection,
    CommunicationRecommendationOutcome,
    SignalType,
)
from gateway.contact_memory.store import ContactMemoryStore


DAY = 86_400.0


def _id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _event(name: str, *, at: float = 100.0, kind: CommunicationKind = CommunicationKind.TEXT,
           actor: CommunicationActorRole = CommunicationActorRole.CONTACT,
           reaction: CommunicationReactionSubtype | None = None) -> CommunicationEvent:
    return CommunicationEvent(
        event_id=_id(f"event:{name}"), platform="synthetic", source_id=_id(f"source:{name}"),
        occurred_at=at, direction=CommunicationDirection.INBOUND, kind=kind, actor_role=actor,
        reaction_subtype=reaction, text_hash=_id(f"text:{name}") if kind not in {
            CommunicationKind.REACTION_ADD, CommunicationKind.REACTION_REMOVE,
        } else None, text_present=kind not in {
            CommunicationKind.REACTION_ADD, CommunicationKind.REACTION_REMOVE,
        }, text_length=12 if kind not in {
            CommunicationKind.REACTION_ADD, CommunicationKind.REACTION_REMOVE,
        } else 0, provenance="synthetic-fixture",
    )


def _interest(topic: str = "live music", signal: SignalType = SignalType.SPONTANEOUS_RAISE,
              confidence: float = 0.95, method: ProjectionMethod = ProjectionMethod.MODEL) -> InterestProjection:
    return InterestProjection(
        topic=topic, signal_type=signal, valence=InterestValence.POSITIVE,
        confidence=confidence, source_method=method,
    )


def test_projection_is_atomic_idempotent_and_uses_only_interest_event_ledger(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    event = _event("atomic")
    store.ingest_communication_event(event)
    projection = CommunicationProjection(
        interests=(_interest(),),
        entities=(EntityProjection(
            canonical_label="Massive Attack", entity_type="thing", confidence=0.96,
            source_method=ProjectionMethod.MODEL,
        ),),
    )

    first = store.project_communication_event(event.event_id, projection, projector_version="phase-d-v1")
    replay = store.project_communication_event(event.event_id, projection, projector_version="phase-d-v1")

    assert first.inserted and replay.deduplicated
    assert first.identities == replay.identities
    with sqlite3.connect(store.path) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert con.execute("SELECT count(*) FROM interest_event").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM projected_entity").fetchone()[0] == 1
        assert not ({"projection_interest", "topic_event", "topic_score"} & tables)


def test_projection_failure_rolls_back_interest_entity_and_receipt(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    event = _event("rollback")
    store.ingest_communication_event(event)
    with sqlite3.connect(store.path) as con:
        con.execute("""CREATE TRIGGER fail_projected_entity BEFORE INSERT ON projected_entity
                       BEGIN SELECT RAISE(ABORT, 'injected projection failure'); END""")

    with pytest.raises(ValueError, match="projection transaction failed"):
        store.project_communication_event(
            event.event_id,
            CommunicationProjection(
                interests=(_interest(),),
                entities=(EntityProjection(
                    canonical_label="Portishead", entity_type="thing", confidence=0.99,
                    source_method=ProjectionMethod.MODEL,
                ),),
            ),
            projector_version="phase-d-v1",
        )
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT count(*) FROM interest_event").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM projected_entity").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM communication_projection_receipt").fetchone()[0] == 0


def test_version_reprojection_deactivates_only_prior_canonical_rows(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    legacy = store.record_interest_event(
        topic_text="photography", signal_type=SignalType.ENTHUSIASM,
        valence=InterestValence.POSITIVE, source_id="legacy:extractor", now=50.0,
    )
    event = _event("versioned")
    store.ingest_communication_event(event)
    v1 = store.project_communication_event(
        event.event_id, CommunicationProjection(interests=(_interest("live music"),)),
        projector_version="phase-d-v1",
    )
    v2 = store.project_communication_event(
        event.event_id, CommunicationProjection(interests=(_interest("concerts"),)),
        projector_version="phase-d-v2",
    )

    assert v1.identities != v2.identities
    with sqlite3.connect(store.path) as con:
        rows = con.execute(
            "SELECT topic_text,projector_version,active FROM interest_event ORDER BY created_at,event_id"
        ).fetchall()
        assert (legacy.topic_text, None, 1) in rows
        assert ("live music", "phase-d-v1", 0) in rows
        assert ("concerts", "phase-d-v2", 1) in rows
        receipts = con.execute(
            "SELECT projector_version,active FROM communication_projection_receipt ORDER BY projector_version"
        ).fetchall()
        assert receipts == [("phase-d-v1", 0), ("phase-d-v2", 1)]


def test_batch_replay_is_chronological_and_identity_is_semantic_keyed(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    later, earlier = _event("later", at=200.0), _event("earlier", at=100.0)
    store.ingest_communication_event(later)
    store.ingest_communication_event(earlier)
    results = store.project_communication_events(
        [(later.event_id, CommunicationProjection(interests=(_interest("jazz"),))),
         (earlier.event_id, CommunicationProjection(interests=(_interest("jazz"),)))],
        projector_version="phase-d-v1",
    )

    assert [result.event_id for result in results] == [earlier.event_id, later.event_id]
    with sqlite3.connect(store.path) as con:
        rows = con.execute(
            "SELECT communication_event_id,replay_sequence FROM communication_projection_receipt "
            "WHERE active=1 ORDER BY replay_sequence"
        ).fetchall()
        assert rows == [(earlier.event_id, 0), (later.event_id, 1)]
        ids = [row[0] for row in con.execute(
            "SELECT event_id FROM interest_event WHERE projector_version='phase-d-v1' ORDER BY event_id"
        )]
    assert len(ids) == len(set(ids)) == 2


@pytest.mark.parametrize("fold_before_removal", [False, True])
def test_retraction_reverses_interest_without_caller_reprojection(
    tmp_path: Path, fold_before_removal: bool,
) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    target = _event(
        "reaction-add", kind=CommunicationKind.REACTION_ADD,
        reaction=CommunicationReactionSubtype.LOVE,
    )
    target_source = _id("reacted-message")
    relation = CommunicationRelation(
        relation_id=_id("reaction-relation"), event_id=target.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source, target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    store.ingest_communication_event(target, relations=(relation,))
    store.project_communication_event(
        target.event_id,
        CommunicationProjection(interests=(_interest(
            "live music", SignalType.ENGAGED_MENTION, confidence=1.0,
            method=ProjectionMethod.DETERMINISTIC,
        ),)),
        projector_version="phase-d-v1",
    )
    if fold_before_removal:
        store.fold_unfolded_interest_events(now=300.0)
        before = store.list_interests()
        assert len(before) == 1 and before[0].raw_score > 0

    removal = _event(
        "reaction-remove", at=400.0, kind=CommunicationKind.REACTION_REMOVE,
        reaction=CommunicationReactionSubtype.LOVE,
    )
    removal_relation = CommunicationRelation(
        relation_id=_id("removal-relation"), event_id=removal.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source, target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    store.retract_communication_event(
        removal, target_event_id=target.event_id, relations=(removal_relation,),
    )
    assert store.list_interests() == []
    with sqlite3.connect(store.path) as con:
        assert con.execute(
            "SELECT active FROM interest_event WHERE origin_communication_event_id=?",
            (target.event_id,),
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT active FROM communication_projection_receipt WHERE communication_event_id=?",
            (target.event_id,),
        ).fetchone()[0] == 0


def test_reaction_removal_before_add_prevents_all_later_semantic_state(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    target = _event(
        "pending-reaction-add", kind=CommunicationKind.REACTION_ADD,
        reaction=CommunicationReactionSubtype.LOVE,
    )
    target_source = _id("pending-reacted-message")
    removal = _event(
        "pending-reaction-remove", at=90.0, kind=CommunicationKind.REACTION_REMOVE,
        reaction=CommunicationReactionSubtype.LOVE,
    )
    def relation(name: str, event_id: str) -> CommunicationRelation:
        return CommunicationRelation(
            relation_id=_id(name), event_id=event_id,
            relation_type=CommunicationRelationType.REACTION_TO,
            target_source_id=target_source,
            target_actor_role=CommunicationActorRole.COUNTERPART,
        )

    store.retract_communication_event(
        removal, target_event_id=target.event_id,
        relations=(relation("pending-removal-relation", removal.event_id),),
    )
    store.ingest_communication_event(
        target, relations=(relation("pending-add-relation", target.event_id),),
    )
    result = store.project_communication_event(
        target.event_id,
        CommunicationProjection(interests=(_interest(
            "live music", SignalType.ENGAGED_MENTION, confidence=1.0,
            method=ProjectionMethod.DETERMINISTIC,
        ),)),
        projector_version="phase-d-v1",
    )

    assert result.retracted
    assert store.list_interests() == []
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT count(*) FROM interest_event").fetchone()[0] == 0
        assert con.execute(
            "SELECT projection_count,active FROM communication_projection_receipt"
        ).fetchone() == (0, 1)


def test_same_topic_projection_supersession_preserves_exact_legacy_baseline(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    with sqlite3.connect(store.path) as con:
        con.execute(
            """INSERT INTO interest(
              interest_id,topic,parent_id,raw_score,last_evidence_at,evidence_count,valence,
              half_life_days,state,ts_alpha,ts_beta,created_at,updated_at,retired_at
            ) VALUES('legacy-music','music',NULL,4.2,50,7,'positive',180,'active',3.5,2.25,10,50,NULL)"""
        )
    baseline = store.list_interests()
    event = _event("legacy-same-topic", at=100.0)
    store.ingest_communication_event(event)

    store.project_communication_event(
        event.event_id, CommunicationProjection(interests=(_interest("music"),)),
        projector_version="phase-d-v1", now=110.0,
    )
    assert store.list_interests() == baseline
    store.fold_unfolded_interest_events(now=120.0)
    assert store.list_interests() != baseline

    store.project_communication_event(
        event.event_id, CommunicationProjection(interests=(_interest("music"),)),
        projector_version="phase-d-v2", now=130.0,
    )
    assert store.list_interests() == baseline
    with sqlite3.connect(store.path) as con:
        row = con.execute("SELECT * FROM interest WHERE interest_id='legacy-music'").fetchone()
        assert row is not None


def test_v7_migration_backfills_folded_projection_baseline_once(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    target = _event(
        "migrated-reaction-add", at=2 * DAY, kind=CommunicationKind.REACTION_ADD,
        reaction=CommunicationReactionSubtype.LOVE,
    )
    target_source = _id("migrated-reacted-message")
    relation = CommunicationRelation(
        relation_id=_id("migrated-reaction-relation"), event_id=target.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source, target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    with sqlite3.connect(store.path) as con:
        con.execute(
            """INSERT INTO interest(
              interest_id,topic,parent_id,raw_score,last_evidence_at,evidence_count,valence,
              half_life_days,state,ts_alpha,ts_beta,created_at,updated_at,retired_at
            ) VALUES('legacy-music','music',NULL,4.2,?,7,'positive',180,'active',
                     3.5,2.25,10,?,NULL)""",
            (DAY, DAY),
        )
        con.execute(
            """INSERT INTO interest_event(
              event_id,topic_text,signal_type,valence,source_id,created_at,folded_at
            ) VALUES('legacy-music-event','music','enthusiasm','positive','legacy',?,?)""",
            (DAY, DAY),
        )
    store.ingest_communication_event(target, relations=(relation,))
    store.project_communication_event(
        target.event_id,
        CommunicationProjection(interests=(_interest(
            "music", SignalType.ENGAGED_MENTION, confidence=1.0,
            method=ProjectionMethod.DETERMINISTIC,
        ),)),
        projector_version="phase-d-v1",
    )
    store.fold_unfolded_interest_events(now=3 * DAY)
    with sqlite3.connect(store.path) as con:
        assert con.execute(
            "SELECT raw_score,evidence_count FROM interest WHERE interest_id='legacy-music'"
        ).fetchone() == pytest.approx((4.6, 8))
        con.execute("DROP TABLE interest_projection_baseline")
        con.execute("UPDATE schema_meta SET value='7' WHERE key='schema_version'")

    class InterruptedMigration(RuntimeError):
        pass

    class InterruptedStore(ContactMemoryStore):
        @staticmethod
        def _backfill_v7_interest_projection_baselines(con: sqlite3.Connection) -> None:
            ContactMemoryStore._backfill_v7_interest_projection_baselines(con)
            raise InterruptedMigration

    with pytest.raises(InterruptedMigration):
        InterruptedStore(tmp_path, "contact")
    with sqlite3.connect(store.path) as con:
        assert con.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "7"
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interest_projection_baseline'"
        ).fetchone() is None

    migrated = ContactMemoryStore(tmp_path, "contact")
    ContactMemoryStore(tmp_path, "contact")
    with sqlite3.connect(store.path) as con:
        baseline = con.execute(
            """SELECT raw_score,last_evidence_at,evidence_count,valence,ts_alpha,ts_beta,
                      state,retired_at FROM interest_projection_baseline WHERE topic='music'"""
        ).fetchone()
        assert baseline is not None
        assert (baseline[0], baseline[1], baseline[2], baseline[4], baseline[5]) == pytest.approx(
            (4.2, DAY, 7, 3.5, 2.25)
        )
        assert (baseline[3], baseline[6], baseline[7]) == ("positive", "active", None)
        assert con.execute(
            "SELECT count(*) FROM interest_projection_baseline WHERE topic='music'"
        ).fetchone()[0] == 1

    migrated.project_communication_event(
        target.event_id,
        CommunicationProjection(interests=(_interest(
            "music", SignalType.ENGAGED_MENTION, confidence=1.0,
            method=ProjectionMethod.DETERMINISTIC,
        ),)),
        projector_version="phase-d-v2",
    )
    migrated.fold_unfolded_interest_events(now=4 * DAY)
    with sqlite3.connect(store.path) as con:
        assert con.execute(
            "SELECT raw_score,evidence_count FROM interest WHERE interest_id='legacy-music'"
        ).fetchone() == pytest.approx((4.6, 8))

    removal = _event(
        "migrated-reaction-remove", at=5 * DAY, kind=CommunicationKind.REACTION_REMOVE,
        reaction=CommunicationReactionSubtype.LOVE,
    )
    removal_relation = CommunicationRelation(
        relation_id=_id("migrated-removal-relation"), event_id=removal.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source, target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    migrated.retract_communication_event(
        removal, target_event_id=target.event_id, relations=(removal_relation,),
    )
    restored = migrated.get_interest("legacy-music")
    assert restored is not None
    assert (
        restored.raw_score, restored.last_evidence_at, restored.evidence_count,
        restored.ts_alpha, restored.ts_beta,
    ) == pytest.approx((4.2, DAY, 7, 3.5, 2.25))
    assert (restored.valence.value, restored.state.value, restored.retired_at) == (
        "positive", "active", None,
    )


def test_retraction_restores_candidate_lifecycle_from_projection_baseline(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    target = _event(
        "lifecycle-reaction-add", at=2 * DAY, kind=CommunicationKind.REACTION_ADD,
        reaction=CommunicationReactionSubtype.LOVE,
    )
    target_source = _id("lifecycle-reacted-message")
    relation = CommunicationRelation(
        relation_id=_id("lifecycle-reaction-relation"), event_id=target.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source, target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    with sqlite3.connect(store.path) as con:
        con.execute(
            """INSERT INTO interest(
              interest_id,topic,parent_id,raw_score,last_evidence_at,evidence_count,valence,
              half_life_days,state,ts_alpha,ts_beta,created_at,updated_at,retired_at
            ) VALUES('candidate-music','music',NULL,1.2,?,7,'positive',180,'candidate',
                     1,1,10,?,NULL)""",
            (DAY, DAY),
        )
        con.execute(
            """INSERT INTO interest_event(
              event_id,topic_text,signal_type,valence,source_id,created_at,folded_at
            ) VALUES('legacy-day','music','enthusiasm','positive','legacy',?,?)""",
            (DAY, DAY),
        )
    store.ingest_communication_event(target, relations=(relation,))
    store.project_communication_event(
        target.event_id,
        CommunicationProjection(interests=(_interest(
            "music", SignalType.ENGAGED_MENTION, confidence=1.0,
            method=ProjectionMethod.DETERMINISTIC,
        ),)),
        projector_version="phase-d-v1",
    )
    store.fold_unfolded_interest_events(now=3 * DAY)
    store.apply_interest_maintenance_batch(now=3 * DAY)
    promoted = store.get_interest("candidate-music")
    assert promoted is not None and promoted.state.value == "active"

    removal = _event(
        "lifecycle-reaction-remove", at=4 * DAY, kind=CommunicationKind.REACTION_REMOVE,
        reaction=CommunicationReactionSubtype.LOVE,
    )
    removal_relation = CommunicationRelation(
        relation_id=_id("lifecycle-removal-relation"), event_id=removal.event_id,
        relation_type=CommunicationRelationType.REACTION_TO,
        target_source_id=target_source, target_actor_role=CommunicationActorRole.COUNTERPART,
    )
    store.retract_communication_event(
        removal, target_event_id=target.event_id, relations=(removal_relation,),
    )
    restored = store.get_interest("candidate-music")
    assert restored is not None
    assert (restored.raw_score, restored.evidence_count, restored.state.value, restored.retired_at) == (
        1.2, 7, "candidate", None,
    )


def test_actor_reply_reaction_and_confidence_rules_are_mechanical(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    counterpart = _event("counterpart", actor=CommunicationActorRole.COUNTERPART)
    store.ingest_communication_event(counterpart)
    with pytest.raises(ValueError, match="contact-authored"):
        store.project_communication_event(
            counterpart.event_id, CommunicationProjection(interests=(_interest(),)),
            projector_version="phase-d-v1",
        )

    reply = _event("reply", kind=CommunicationKind.REPLY)
    store.ingest_communication_event(reply, relations=(CommunicationRelation(
        relation_id=_id("reply-relation"), event_id=reply.event_id,
        relation_type=CommunicationRelationType.REPLY_TO,
        target_source_id=counterpart.source_id,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    ),))
    with pytest.raises(ValueError, match="neutral"):
        store.project_communication_event(
            reply.event_id, CommunicationProjection(interests=(_interest(
                signal=SignalType.ENTHUSIASM,
            ),)), projector_version="phase-d-v1",
        )
    with pytest.raises(ValueError, match="confidence"):
        store.project_communication_event(
            reply.event_id, CommunicationProjection(interests=(_interest(
                signal=SignalType.NEUTRAL_ACK, confidence=0.89,
            ),)), projector_version="phase-d-v1",
        )


def test_entity_aliases_merge_per_contact_without_global_registry(tmp_path: Path) -> None:
    left = ContactMemoryStore(tmp_path / "left", "left")
    right = ContactMemoryStore(tmp_path / "right", "right")
    first, second = _event("entity-one"), _event("entity-two", at=200.0)
    left.ingest_communication_event(first)
    left.ingest_communication_event(second)
    left.project_communication_event(
        first.event_id, CommunicationProjection(entities=(EntityProjection(
            canonical_label="  Massive   Attack ", entity_type="thing", confidence=0.95,
            source_method=ProjectionMethod.MODEL,
        ),)), projector_version="phase-d-v1",
    )
    left.project_communication_event(
        second.event_id, CommunicationProjection(entities=(EntityProjection(
            canonical_label="massive attack", entity_type="thing", confidence=0.95,
            source_method=ProjectionMethod.MODEL,
        ),)), projector_version="phase-d-v1",
    )

    with sqlite3.connect(left.path) as con:
        rows = con.execute(
            "SELECT normalized_key,count(*) FROM projected_entity WHERE active=1 GROUP BY normalized_key"
        ).fetchall()
        assert rows == [("massive attack", 2)]
    assert not right.path.parent.joinpath("registry.sqlite3").exists()
    with sqlite3.connect(right.path) as con:
        assert con.execute("SELECT count(*) FROM projected_entity").fetchone()[0] == 0


def test_interest_merge_alias_prevents_reprojection_from_recreating_absorbed_topic(
    tmp_path: Path,
) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    first, second = _event("merge-one", at=100.0), _event("merge-two", at=200.0)
    store.ingest_communication_event(first)
    store.ingest_communication_event(second)
    store.project_communication_event(
        first.event_id, CommunicationProjection(interests=(_interest("hip hop"),)),
        projector_version="phase-d-v1",
    )
    store.project_communication_event(
        second.event_id, CommunicationProjection(interests=(_interest("rap music"),)),
        projector_version="phase-d-v1",
    )
    store.fold_unfolded_interest_events(now=300.0)
    interests = {item.topic: item for item in store.list_interests()}
    store.apply_interest_maintenance_batch(
        merges=((interests["hip hop"].interest_id, interests["rap music"].interest_id),),
        now=400.0,
    )
    replay = _event("merge-replay", at=500.0)
    store.ingest_communication_event(replay)
    store.project_communication_event(
        replay.event_id, CommunicationProjection(interests=(_interest("rap music"),)),
        projector_version="phase-d-v1",
    )
    assert store.unfolded_interest_events()[0].topic_text == "hip hop"


def test_recommendation_follow_through_and_callback_linkage(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    proposed = _event("recommendation", kind=CommunicationKind.RECOMMENDATION,
                      actor=CommunicationActorRole.COUNTERPART)
    store.ingest_communication_event(proposed)
    initial = store.project_communication_event(
        proposed.event_id,
        CommunicationProjection(recommendations=(RecommendationProjection(
            semantic_key="album:mezzanine", topic="music",
            recommendation="listen to mezzanine", outcome=CommunicationRecommendationOutcome.PROPOSED,
            confidence=1.0, source_method=ProjectionMethod.DETERMINISTIC,
            explicit_linkage=True,
        ),)), projector_version="phase-d-v1",
    )
    recommendation_id = initial.identities[0]

    follow = _event("follow", at=200.0, kind=CommunicationKind.FOLLOW_THROUGH)
    store.ingest_communication_event(follow, relations=(CommunicationRelation(
        relation_id=_id("follow-relation"), event_id=follow.event_id,
        relation_type=CommunicationRelationType.FOLLOWS_THROUGH,
        target_source_id=proposed.source_id,
        target_actor_role=CommunicationActorRole.COUNTERPART,
    ),))
    store.project_communication_event(
        follow.event_id,
        CommunicationProjection(recommendations=(RecommendationProjection(
            semantic_key="album:mezzanine", outcome=CommunicationRecommendationOutcome.FULFILLED,
            confidence=1.0, source_method=ProjectionMethod.DETERMINISTIC,
            explicit_linkage=True,
        ),)), projector_version="phase-d-v1",
    )

    callback_one = _event("callback-one", at=DAY, kind=CommunicationKind.CALLBACK)
    callback_two = _event("callback-two", at=2 * DAY, kind=CommunicationKind.CALLBACK)
    store.ingest_communication_event(callback_one)
    store.ingest_communication_event(callback_two)
    store.project_communication_event(
        callback_two.event_id,
        CommunicationProjection(callbacks=(CallbackProjection(
            semantic_key="mezzanine-rain-joke", canonical_label="mezzanine rain joke",
            confidence=0.95, source_method=ProjectionMethod.MODEL,
            supporting_event_ids=(callback_one.event_id, callback_two.event_id),
        ),)), projector_version="phase-d-v1",
    )

    with sqlite3.connect(store.path) as con:
        recommendation = con.execute(
            "SELECT status FROM recommendation WHERE recommendation_id=?", (recommendation_id,)
        ).fetchone()
        assert recommendation == ("fulfilled",)
        lifecycle = con.execute(
            "SELECT outcome FROM communication_recommendation_event ORDER BY event_id"
        ).fetchall()
        assert {row[0] for row in lifecycle} == {"proposed", "fulfilled"}
        callback = con.execute(
            "SELECT privacy,active FROM semantic_callback"
        ).fetchone()
        assert callback == ("restricted", 1)
        assert con.execute(
            "SELECT count(*) FROM semantic_callback_support"
        ).fetchone()[0] == 2


def test_inferred_fulfillment_unrelated_linkage_and_raw_artifacts_are_rejected(tmp_path: Path) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    event = _event("privacy", kind=CommunicationKind.RECOMMENDATION)
    store.ingest_communication_event(event)
    bad = (
        CommunicationProjection(interests=(_interest("https://secret.example/token=x"),)),
        CommunicationProjection(entities=(EntityProjection(
            canonical_label="/Users/name/private.jpg", entity_type="thing", confidence=0.99,
            source_method=ProjectionMethod.MODEL,
        ),)),
        CommunicationProjection(recommendations=(RecommendationProjection(
            semantic_key="secret", topic="music", recommendation="open https://secret.example/?token=x",
            outcome=CommunicationRecommendationOutcome.PROPOSED, confidence=0.99,
            source_method=ProjectionMethod.MODEL, explicit_linkage=False,
        ),)),
        CommunicationProjection(recommendations=(RecommendationProjection(
            semantic_key="secret", outcome=CommunicationRecommendationOutcome.FULFILLED,
            confidence=0.99, source_method=ProjectionMethod.MODEL, explicit_linkage=False,
        ),)),
    )
    for projection in bad:
        with pytest.raises(ValueError):
            store.project_communication_event(
                event.event_id, projection, projector_version="phase-d-v1",
            )
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT count(*) FROM communication_projection_receipt").fetchone()[0] == 0
        payload = " ".join(str(value) for table in (
            "interest_event", "projected_entity", "recommendation", "semantic_callback",
        ) for row in con.execute(f"SELECT * FROM {table}") for value in row if value is not None)
        assert "https://" not in payload and "/Users/" not in payload and "token=x" not in payload


@pytest.mark.parametrize(
    "label",
    ["550e8400-e29b-41d4-a716-446655440000", "sk-proj-abcdefghijklmnopqrstuvwxyz012345"],
)
def test_semantic_entity_labels_reject_opaque_identifiers(tmp_path: Path, label: str) -> None:
    store = ContactMemoryStore(tmp_path, "contact")
    event = _event(f"opaque-label:{label}")
    store.ingest_communication_event(event)
    with pytest.raises(ValueError, match="raw artifacts or credentials"):
        store.project_communication_event(
            event.event_id,
            CommunicationProjection(entities=(EntityProjection(
                canonical_label=label, entity_type="thing", confidence=0.99,
                source_method=ProjectionMethod.MODEL,
            ),)),
            projector_version="phase-d-v1",
        )


@pytest.mark.parametrize("label", ["Massive Attack", "The Bear", "Prospect Park"])
def test_semantic_entity_labels_preserve_legitimate_names(tmp_path: Path, label: str) -> None:
    store = ContactMemoryStore(tmp_path, f"contact:{label}")
    event = _event(f"legitimate-label:{label}")
    store.ingest_communication_event(event)
    result = store.project_communication_event(
        event.event_id,
        CommunicationProjection(entities=(EntityProjection(
            canonical_label=label, entity_type="thing", confidence=0.99,
            source_method=ProjectionMethod.MODEL,
        ),)),
        projector_version="phase-d-v1",
    )
    assert result.inserted
