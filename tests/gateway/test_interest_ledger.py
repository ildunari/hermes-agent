from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from gateway.contact_memory.extractor import propose_turn_memories
from gateway.contact_memory.qwen3_extractor_worker import _parse
from gateway.contact_memory.schema import (
    SCHEMA_VERSION,
    CommunicationActorRole,
    CommunicationDirection,
    CommunicationEvent,
    CommunicationKind,
    GateDecision,
    Interest,
    InterestEvent,
    InterestState,
    InterestValence,
    ProactiveOutcome,
    ProactiveSend,
    ProactiveSendKind,
    SignalType,
)
from gateway.contact_memory.store import (
    ContactMemoryStore, normalize_interest_topic, opaque_contact_filename,
)


DAY = 86_400.0


def _canonical_text(store: ContactMemoryStore, source: str, *, at: float = 100.0) -> str:
    event = CommunicationEvent(
        event_id=hashlib.sha256(f"canonical:{source}".encode()).hexdigest(),
        platform="synthetic", source_id=hashlib.sha256(source.encode()).hexdigest(),
        occurred_at=at, direction=CommunicationDirection.INBOUND,
        kind=CommunicationKind.TEXT, actor_role=CommunicationActorRole.CONTACT,
        text_hash="a" * 64, text_present=True, text_length=20,
        provenance="synthetic-fixture",
    )
    store.ingest_communication_event(event)
    return event.event_id


def _raw_fact() -> dict[str, object]:
    return {
        "logical_id": "preference:cars",
        "subject_id": "person:contact",
        "predicate": "likes",
        "object_text": "Likes sports cars.",
        "audience": "owner_review",
        "mention_policy": "background",
        "assertion_type": "stated",
        "trust": 0.96,
        "confidence": 0.98,
        "evidence_pointer": "untrusted",
        "metadata": {"category": "preference", "sensitivity": "normal"},
    }


_V2_FIXTURE = Path(__file__).with_name("fixtures") / "contact_memory_v2.sql"
_V2_PRESERVED_TABLES = (
    "fact", "pending_fact", "recommendation", "recall_event", "callback_event",
)


def _install_historical_v2(tmp_path: Path) -> Path:
    path = tmp_path / "contacts" / opaque_contact_filename("contact")
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.executescript(_V2_FIXTURE.read_text())
    return path


def _historical_rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as con:
        return {
            table: [tuple(row) for row in con.execute(f"SELECT * FROM {table}")]
            for table in _V2_PRESERVED_TABLES
        }


def test_v2_to_current_migration_preserves_real_historical_rows_exactly(tmp_path: Path):
    path = _install_historical_v2(tmp_path)
    before = _historical_rows(path)

    reopened = ContactMemoryStore(tmp_path, "contact")
    ContactMemoryStore(tmp_path, "contact")  # A second open must be idempotent.

    assert _historical_rows(path) == before
    with sqlite3.connect(reopened.path) as con:
        assert con.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        assert con.execute(
            "SELECT value FROM schema_meta WHERE key='historical_fixture_marker'"
        ).fetchone()[0] == "preserve-me"
        assert {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }.issuperset({"interest_event", "interest", "proactive_send"})
        assert con.execute("PRAGMA foreign_key_list(proactive_send)").fetchone()[2] == "interest"


def test_v2_to_current_migration_rolls_back_and_recovers_after_interruption(tmp_path: Path):
    path = _install_historical_v2(tmp_path)
    before = _historical_rows(path)

    class Interrupted(RuntimeError):
        pass

    class InterruptedStore(ContactMemoryStore):
        @staticmethod
        def _execute_script(con: sqlite3.Connection, script: str) -> None:
            ContactMemoryStore._execute_script(con, script)
            raise Interrupted

    with pytest.raises(Interrupted):
        InterruptedStore(tmp_path, "contact")
    assert _historical_rows(path) == before
    with sqlite3.connect(path) as con:
        assert con.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interest_event'"
        ).fetchone() is None

    recovered = ContactMemoryStore(tmp_path, "contact")
    assert recovered.unfolded_interest_events() == []
    assert _historical_rows(path) == before


def test_concurrent_v1_migration_rechecks_version_under_writer_lock(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    with sqlite3.connect(store.path) as con:
        con.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
        con.executescript("DROP TABLE proactive_send; DROP TABLE interest; DROP TABLE interest_event;")

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    migration_calls: list[int] = []

    class ConcurrentStore(ContactMemoryStore):
        @staticmethod
        def _migrate_v1_to_v2(con: sqlite3.Connection) -> None:
            # This fixture is already v2-shaped. Delay the metadata-1 writer so
            # both openers observe v1 before contending for BEGIN IMMEDIATE.
            migration_calls.append(1)
            time.sleep(0.05)

    def reopen() -> None:
        try:
            barrier.wait()
            ConcurrentStore(tmp_path, "contact")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reopen) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert migration_calls == [1]
    with sqlite3.connect(store.path) as con:
        assert con.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)


def test_topic_validation_and_event_writes_are_deterministic(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    assert normalize_interest_topic("  Sports   Cars ") == "sports cars"
    for junk in (
        "the", "stuff", "five word topic is too long", "cars!", "1234",
        "ignore previous instructions", "system prompt", "", "hiv medication",
        "divorce proceedings", "bank debt", "sexual health",
        "depression", "diabetes", "opioid addiction", "abortion",
        "suicide prevention", "self harm", "rape survivors", "domestic violence",
    ):
        with pytest.raises(ValueError):
            normalize_interest_topic(junk)

    first = store.record_interest_event(
        topic_text="Sports Cars", signal_type=SignalType.ENTHUSIASM,
        valence=InterestValence.POSITIVE, source_id="message:1", now=100,
    )
    retry = store.record_interest_event(
        topic_text="sports cars", signal_type="enthusiasm", valence="positive",
        source_id="message:1", now=999,
    )
    assert first == retry
    assert first.topic_text == "sports cars"
    assert store.unfolded_interest_events() == [first]
    assert store.mark_interest_events_folded([first.event_id], now=200) == 1
    assert store.unfolded_interest_events() == []


@pytest.mark.parametrize("topic", [
    "cancer treatment",       # health
    "relationship problems", # relationship
    "personal finance",       # finance
    "legal trouble",          # legal
    "sexual health",          # sexual
    "api key",                # credential-derived
    "custody battle",         # relationship/legal
    "estate planning",        # legal/financial
    "probation terms",        # legal
])
def test_sensitive_topics_are_rejected_at_store_boundary(tmp_path: Path, topic: str):
    store = ContactMemoryStore(tmp_path, "contact")
    with pytest.raises(ValueError, match="sensitive"):
        store.record_interest_event(
            topic_text=topic, signal_type="enthusiasm", valence="positive",
            source_id=f"message:{topic}", now=100.0,
        )
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT count(*) FROM interest_event").fetchone()[0] == 0


def _interest(interest_id: str, topic: str, parent_id: str | None = None) -> Interest:
    return Interest(
        interest_id=interest_id, topic=topic, parent_id=parent_id, raw_score=3.0,
        last_evidence_at=100.0, evidence_count=1, valence=InterestValence.POSITIVE,
        half_life_days=30.0, state=InterestState.ACTIVE, ts_alpha=1.0, ts_beta=1.0,
        created_at=100.0, updated_at=100.0, retired_at=None,
    )


def test_interest_reparent_cannot_create_third_taxonomy_level(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    parent = store.put_interest(_interest("parent", "motorsports"))
    store.put_interest(_interest("child", "sports cars", parent.interest_id))
    other_parent = store.put_interest(_interest("other", "engineering"))

    with pytest.raises(ValueError, match="children must remain top-level"):
        store.put_interest(replace(parent, parent_id=other_parent.interest_id, updated_at=101.0))
    assert store.get_interest("parent") == parent
    child = store.get_interest("child")
    assert child is not None and child.parent_id == parent.interest_id


def test_duplicate_live_topic_has_normalized_api_error(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    store.put_interest(_interest("one", "motorsports"))
    with pytest.raises(ValueError, match="topic already has a live interest"):
        store.put_interest(_interest("two", "motorsports"))


def test_effective_score_is_lazy_and_interest_api_filters_without_storing_decay(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    interest = Interest(
        interest_id="cars", topic="cars", parent_id=None, raw_score=8.0,
        last_evidence_at=100.0, evidence_count=3, valence=InterestValence.POSITIVE,
        half_life_days=10.0, state=InterestState.ACTIVE, ts_alpha=2.0, ts_beta=1.0,
        created_at=50.0, updated_at=100.0, retired_at=None,
    )
    store.put_interest(interest)
    read = store.get_interest("cars")
    assert read is not None
    assert read.effective_score(100.0 + 10 * DAY) == pytest.approx(4.0)
    assert store.list_interests(
        state=InterestState.ACTIVE, valence=InterestValence.POSITIVE,
        min_effective_score=4.1, now=100.0 + 10 * DAY,
    ) == []
    assert store.list_interests(min_effective_score=3.9, now=100.0 + 10 * DAY) == [read]
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT raw_score FROM interest").fetchone()[0] == 8.0


def test_proactive_send_api_round_trips_json_and_outcome(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    send = ProactiveSend(
        send_id="send-1", interest_id=None, kind=ProactiveSendKind.CHECKIN,
        candidate_json=json.dumps({"topic": "checkin"}),
        gate_decision=GateDecision.SENT, gate_reason="passed", sent_at=100.0,
        outcome=None, outcome_at=None, created_at=99.0,
    )
    assert store.record_proactive_send(send) == send
    retry = store.record_proactive_send(send)
    assert retry == send
    updated = store.set_proactive_send_outcome(
        "send-1", ProactiveOutcome.ENGAGED, now=120.0
    )
    assert updated.outcome is ProactiveOutcome.ENGAGED
    assert updated.outcome_at == 120.0
    assert store.recent_proactive_sends(since=90.0) == [updated]


def test_proactive_send_rejects_impossible_state_transitions(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    suppressed = ProactiveSend(
        send_id="suppressed", interest_id=None, kind=ProactiveSendKind.CHECKIN,
        candidate_json="{}", gate_decision=GateDecision.SUPPRESSED,
        gate_reason="no_material", sent_at=None, outcome=None, outcome_at=None,
        created_at=10.0,
    )
    store.record_proactive_send(suppressed)
    with pytest.raises(ValueError, match="only sent"):
        store.set_proactive_send_outcome("suppressed", ProactiveOutcome.ENGAGED, now=20.0)
    with pytest.raises(ValueError, match="cannot have sent_at"):
        store.record_proactive_send(ProactiveSend(
            send_id="impossible", interest_id=None, kind=ProactiveSendKind.CHECKIN,
            candidate_json="{}", gate_decision=GateDecision.SUPPRESSED,
            gate_reason="no_material", sent_at=10.0, outcome=None, outcome_at=None,
            created_at=9.0,
        ))
    with pytest.raises(ValueError, match="cannot have outcomes"):
        store.record_proactive_send(ProactiveSend(
            send_id="suppressed-outcome", interest_id=None,
            kind=ProactiveSendKind.CHECKIN, candidate_json="{}",
            gate_decision=GateDecision.SUPPRESSED, gate_reason="no_material",
            sent_at=None, outcome=ProactiveOutcome.ENGAGED, outcome_at=11.0,
            created_at=9.0,
        ))
    with pytest.raises(ValueError, match="outcomes require outcome_at"):
        store.record_proactive_send(ProactiveSend(
            send_id="outcome-without-time", interest_id=None,
            kind=ProactiveSendKind.CHECKIN, candidate_json="{}",
            gate_decision=GateDecision.SENT, gate_reason="passed", sent_at=10.0,
            outcome=ProactiveOutcome.ENGAGED, outcome_at=None, created_at=9.0,
        ))
    sent = ProactiveSend(
        send_id="sent", interest_id=None, kind=ProactiveSendKind.CHECKIN,
        candidate_json="{}", gate_decision=GateDecision.SENT,
        gate_reason="passed", sent_at=100.0, outcome=None, outcome_at=None,
        created_at=99.0,
    )
    store.record_proactive_send(sent)
    with pytest.raises(ValueError, match="cannot precede"):
        store.set_proactive_send_outcome("sent", ProactiveOutcome.ENGAGED, now=99.0)
    with sqlite3.connect(store.path) as con:
        for outcome, outcome_at in (("engaged", None), (None, 110.0)):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(
                    ("INSERT INTO proactive_send(send_id,interest_id,kind,candidate_json,"
                     "gate_decision,gate_reason,sent_at,outcome,outcome_at,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)"),
                    (f"direct-{outcome}", None, "checkin", "{}", "sent", "passed",
                     100.0, outcome, outcome_at, 99.0),
                )
        for send_id, sent_at, outcome, outcome_at in (
            ("direct-suppressed-sent", 100.0, None, None),
            ("direct-suppressed-outcome", None, "engaged", 110.0),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(
                    ("INSERT INTO proactive_send(send_id,interest_id,kind,candidate_json,"
                     "gate_decision,gate_reason,sent_at,outcome,outcome_at,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)"),
                    (send_id, None, "checkin", "{}", "suppressed", "blocked",
                     sent_at, outcome, outcome_at, 99.0),
                )


def test_proactive_send_rejects_orphaned_interest_in_api_and_schema(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    orphan = ProactiveSend(
        send_id="orphan", interest_id="missing", kind=ProactiveSendKind.INTEREST_SHARE,
        candidate_json="{}", gate_decision=GateDecision.SENT, gate_reason="passed",
        sent_at=100.0, outcome=None, outcome_at=None, created_at=99.0,
    )
    with pytest.raises(ValueError, match="unknown interest"):
        store.record_proactive_send(orphan)

    with sqlite3.connect(store.path) as con:
        con.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            con.execute(
                ("INSERT INTO proactive_send(send_id,interest_id,kind,candidate_json,"
                     "gate_decision,gate_reason,sent_at,outcome,outcome_at,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)"),
                ("direct-orphan", "missing", "interest_share", "{}", "sent",
                 "passed", 100.0, None, None, 99.0),
            )


def test_interest_mutator_timestamps_reject_nonfinite_without_changes(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    event = store.record_interest_event(
        topic_text="motorsports", signal_type="enthusiasm", valence="positive",
        source_id="message:finite", now=100.0,
    )
    original_interest = store.put_interest(_interest("motorsports", "motorsports"))
    send = ProactiveSend(
        send_id="finite-send", interest_id="motorsports",
        kind=ProactiveSendKind.INTEREST_SHARE, candidate_json="{}",
        gate_decision=GateDecision.SENT, gate_reason="passed", sent_at=100.0,
        outcome=None, outcome_at=None, created_at=99.0,
    )
    store.record_proactive_send(send)

    for invalid in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            store.mark_interest_events_folded([event.event_id], now=invalid)
        with pytest.raises(ValueError, match="finite"):
            store.update_interest_bandit("motorsports", alpha_delta=1.0, now=invalid)
        with pytest.raises(ValueError, match="finite"):
            store.set_proactive_send_outcome("finite-send", "engaged", now=invalid)

    assert store.unfolded_interest_events() == [event]
    assert store.get_interest("motorsports") == original_interest
    assert store.get_proactive_send("finite-send") == send


def test_recent_proactive_send_query_has_matching_index(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    with sqlite3.connect(store.path) as con:
        columns = [
            row[2] for row in con.execute("PRAGMA index_info(proactive_send_recent)")
        ]
    assert columns == ["created_at", "gate_decision"]


def test_message_length_baseline_is_rolling_idempotent_and_stores_no_text(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    for index, length in enumerate((10, 12, 14), start=1):
        result = store.record_contact_message_length(f"message:{index}", length)
        assert not result.is_long_reply
    result = store.record_contact_message_length("message:4", 30)
    retry = store.record_contact_message_length("message:4", 9999)
    assert result.is_long_reply
    assert retry == result
    assert result.baseline_median == 12.0
    assert store.contact_message_length_baseline().sample_count == 4

    marker = "RAW-USER-TEXT-MUST-NOT-BE-STORED"
    store.record_contact_message_length("message:5", len(marker))
    with sqlite3.connect(store.path) as con:
        values = " ".join(row[0] for row in con.execute(
            "SELECT value FROM schema_meta"
        ).fetchall())
    assert marker not in values


@pytest.mark.asyncio
async def test_length_tracking_never_persists_raw_user_text(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    marker = "RAW-USER-TEXT-MUST-NOT-REACH-THE-LEDGER-91827"

    async def backend(user_text, assistant_text, metadata):
        return {"proposals": [], "interest_events": []}

    await propose_turn_memories(
        store, backend, marker, "assistant text", {"source_id": "message:privacy"}
    )
    with sqlite3.connect(store.path) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert marker.encode() not in store.path.read_bytes()


@pytest.mark.asyncio
async def test_extractor_envelope_keeps_fact_behavior_and_adds_user_interest_events(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    for index in range(3):
        store.record_contact_message_length(f"baseline:{index}", 10)

    class Backend:
        async def extract(self, user_text, assistant_text, metadata):
            assert assistant_text == "assistant mentioned private material"
            return {
                "proposals": [_raw_fact()],
                "interest_events": [
                    {"topic": "sports cars", "signal_type": "enthusiasm", "valence": "positive"},
                    # The model cannot choose the deterministic length signal.
                    {"topic": "sports cars", "signal_type": "long_reply", "valence": "positive"},
                ],
            }

    communication_event_id = _canonical_text(store, "message:long")
    ids = await propose_turn_memories(
        store, Backend(), "x" * 30, "assistant mentioned private material",
        {"source_id": "message:long", "communication_event_id": communication_event_id},
    )
    assert len(ids) == 1
    assert store.list_pending()[0]["proposal_id"] == ids[0]
    events = store.unfolded_interest_events()
    assert {(event.signal_type, event.topic_text) for event in events} == {
        (SignalType.ENTHUSIASM, "sports cars"),
        (SignalType.LONG_REPLY, "sports cars"),
    }
    assert all(event.source_id == communication_event_id for event in events)


@pytest.mark.asyncio
async def test_model_cannot_emit_code_owned_or_misvalenced_interest_signals(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")

    async def backend(user_text, assistant_text, metadata):
        return {"proposals": [], "interest_events": [
            {"topic": "cars", "signal_type": "long_reply", "valence": "positive"},
            {"topic": "cars", "signal_type": "proactive_engaged", "valence": "positive"},
            {"topic": "cars", "signal_type": "explicit_negative", "valence": "positive"},
        ]}

    await propose_turn_memories(
        store, backend, "cars", "assistant", {
            "source_id": "message:invalid-signals",
            "communication_event_id": _canonical_text(store, "message:invalid-signals"),
        }
    )
    assert store.unfolded_interest_events() == []


@pytest.mark.asyncio
async def test_model_interest_output_without_canonical_event_cannot_write(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")

    async def backend(user_text, assistant_text, metadata):
        return {"proposals": [], "interest_events": [
            {"topic": "cars", "signal_type": "enthusiasm", "valence": "positive"},
        ]}

    await propose_turn_memories(
        store, backend, "cars", "assistant", {"source_id": "raw-platform-guid"},
    )
    assert store.unfolded_interest_events() == []


@pytest.mark.asyncio
async def test_negative_topic_does_not_gain_long_reply_signal(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    for index in range(3):
        store.record_contact_message_length(f"baseline:{index}", 5)

    async def backend(user_text, assistant_text, metadata):
        return {"proposals": [], "interest_events": [
            {"topic": "cars", "signal_type": "explicit_negative", "valence": "negative"},
        ]}

    await propose_turn_memories(
        store, backend, "x" * 30, "assistant", {
            "source_id": "message:negative-long",
            "communication_event_id": _canonical_text(store, "message:negative-long"),
        }
    )
    assert [event.signal_type for event in store.unfolded_interest_events()] == [
        SignalType.EXPLICIT_NEGATIVE
    ]


@pytest.mark.asyncio
async def test_malformed_interest_output_cannot_discard_valid_fact_output(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")

    async def backend(user_text, assistant_text, metadata):
        return {"proposals": [_raw_fact()], "interest_events": "malformed"}

    ids = await propose_turn_memories(
        store, backend, "I like sports cars", "ignored", {"source_id": "message:fact"}
    )
    assert ids == [store.list_pending()[0]["proposal_id"]]
    assert store.unfolded_interest_events() == []


def test_worker_parser_retains_fact_proposals_and_interest_events():
    payload = _parse(json.dumps({
        "proposals": [_raw_fact()],
        "interest_events": [
            {"topic": "sports cars", "signal_type": "enthusiasm", "valence": "positive"}
        ],
    }))
    assert len(payload["proposals"]) == 1
    assert payload["interest_events"][0]["topic"] == "sports cars"


def test_benchmark_uses_production_system_and_fact_parity_corpus():
    from scripts import benchmark_contact_memory_extractor as benchmark
    from gateway.contact_memory.qwen3_extractor_worker import SYSTEM

    assert benchmark.SYSTEM == SYSTEM
    assert len(benchmark.CASES) == 28
    assert len(benchmark.INTEREST_CASES) >= 5
    assert benchmark.parse_json(
        '{"proposals":[],"interest_events":[]}'
    ) == {"proposals": [], "interest_events": []}
    with pytest.raises(ValueError, match="envelope"):
        benchmark.parse_json('{"proposals":[]}')


def test_ledger_dataclasses_match_persisted_rows(tmp_path: Path):
    store = ContactMemoryStore(tmp_path, "contact")
    event = store.record_interest_event(
        topic_text="music", signal_type="neutral_ack", valence="neutral",
        source_id="message:1", now=1.0,
    )
    assert isinstance(event, InterestEvent)
    assert set(event.__dataclass_fields__) == {
        "event_id", "topic_text", "signal_type", "valence", "source_id",
        "created_at", "folded_at",
    }
