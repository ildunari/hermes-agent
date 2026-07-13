from __future__ import annotations

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gateway.contact_memory.schema import (
    GateDecision,
    Interest,
    InterestState,
    InterestValence,
    ProactiveOutcome,
    ProactiveSend,
    ProactiveSendKind,
)
from gateway.contact_memory.store import ContactMemoryStore
from gateway.proactive_checkin import plan_checkin, push_into_active_hours
from gateway.proactive_scheduler import (
    ContactRoute,
    ProactiveConfig,
    ProactiveScheduler,
    ProactiveStateStore,
    assert_no_profile_conflicts,
    classify_inbound_outcome,
    handle_inbound,
    handle_inbound_async,
)

NOW = 1_800_000_000.0
ROUTE = {
    "platform": "bluebubbles",
    "chat_type": "dm",
    "chat_id": "iMessage;-;+15555550123",
    "user_id": "+15555550123",
    "session_id": "parent",
}


def config(**overrides) -> ProactiveConfig:
    values = {**ProactiveConfig().__dict__, "enabled": True, **overrides}
    cfg = ProactiveConfig(**values)
    cfg.validate()
    return cfg


def contact_route(*, profile: str = "poke", contact_id: str = "contact-a") -> ContactRoute:
    return ContactRoute(
        contact_id=contact_id, profile_name=profile, timezone="America/New_York",
        principal="guest" if profile == "guest" else "owner", chat_type="dm",
        chat_id=str(ROUTE["chat_id"]), user_id=str(ROUTE["user_id"]), session_id="parent",
    )


def make_interest(
    store: ContactMemoryStore,
    interest_id: str = "cars",
    *,
    topic: str = "sports cars",
    parent_id: str | None = None,
    score: float = 4.0,
) -> Interest:
    return store.put_interest(Interest(
        interest_id=interest_id,
        topic=topic,
        parent_id=parent_id,
        raw_score=score,
        last_evidence_at=NOW,
        evidence_count=4,
        valence=InterestValence.POSITIVE,
        half_life_days=90,
        state=InterestState.ACTIVE,
        ts_alpha=2,
        ts_beta=1,
        created_at=NOW - 10,
        updated_at=NOW,
        retired_at=None,
    ))


def register_messages(
    state: ProactiveStateStore,
    *,
    contact_id: str = "contact-a",
    count: int = 5,
    start: float = NOW - 10_000,
    serious: bool = False,
) -> str:
    for index in range(count):
        state.register_inbound(
            profile="poke",
            contact_id=contact_id,
            route=ROUTE,
            timezone_name="America/New_York",
            source_id=f"m-{index}",
            received_at=start + index,
            serious=serious and index == count - 1,
        )
    return state.contact_key(contact_id)


def sent(
    store: ContactMemoryStore,
    send_id: str,
    *,
    interest_id: str | None = "cars",
    when: float,
    kind: ProactiveSendKind = ProactiveSendKind.INTEREST_SHARE,
    outcome: ProactiveOutcome | None = None,
) -> ProactiveSend:
    return store.record_proactive_send(ProactiveSend(
        send_id=send_id,
        interest_id=interest_id,
        kind=kind,
        candidate_json=json.dumps({"topic": "sports cars", "item": send_id}),
        gate_decision=GateDecision.SENT,
        gate_reason="test",
        sent_at=when,
        outcome=outcome,
        outcome_at=when + 1 if outcome else None,
        created_at=when,
    ))


def test_config_is_surfaced_but_phase3_forces_dry_run():
    cfg = ProactiveConfig.from_mapping({
        "agent": {"proactive": {
            "enabled": True,
            "dry_run": False,
            "transport": "bluebubbles",
            "active_hours": {"start": "09:00", "end": "21:30"},
        }}
    })
    assert cfg.enabled is True
    assert cfg.dry_run is True
    with pytest.raises(ValueError, match="structurally dry-run"):
        config(dry_run=False)
    with pytest.raises(ValueError, match="cannot be below 48"):
        config(min_gap_hours=12)


def test_contact_local_active_hours_and_jitter_survive_dst():
    zone = ZoneInfo("America/New_York")
    before_open = datetime(2026, 3, 8, 3, 30, tzinfo=zone).timestamp()
    result = push_into_active_hours(
        before_open,
        timezone_name="America/New_York",
        active_start="09:00",
        active_end="21:30",
        jitter_minutes=45,
        jitter_key="contact-a",
    )
    local = datetime.fromtimestamp(result, zone)
    assert local.date() == datetime(2026, 3, 8, tzinfo=zone).date()
    assert (local.hour, local.minute) >= (9, 0)
    assert (local.hour, local.minute) <= (9, 45)
    assert result == push_into_active_hours(
        before_open,
        timezone_name="America/New_York",
        active_start="09:00",
        active_end="21:30",
        jitter_minutes=45,
        jitter_key="contact-a",
    )


def test_checkin_plan_is_deterministic_contact_local_and_three_to_six_hours():
    anchor = datetime(2026, 1, 5, 10, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    first = plan_checkin(
        contact_key="abc", kind="serious", last_user_ts=anchor,
        reason="hard conversation", timezone_name="America/New_York",
    )
    second = plan_checkin(
        contact_key="abc", kind="serious", last_user_ts=anchor,
        reason="hard conversation", timezone_name="America/New_York",
    )
    assert first == second
    assert 3 * 3600 <= first.send_at_ts - anchor <= 6 * 3600


def test_eligibility_requires_five_unique_inbounds_in_fourteen_days(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    key = register_messages(state, count=4)
    memory = tmp_path / "contact-memory"
    make_interest(ContactMemoryStore(memory, "contact-a"))
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=memory,
        config=config(), profile="poke",
    )
    assert scheduler.tick(now=NOW)["armed"] == 0
    state.register_inbound(
        profile="poke", contact_id="contact-a", route=ROUTE,
        timezone_name="America/New_York", source_id="m-4", received_at=NOW - 5,
    )
    assert scheduler.tick(now=NOW)["armed"] == 1
    assert state.has_pending_slot(key)


def test_cancel_on_inbound_revokes_even_a_claim(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    key = register_messages(state)
    slot_id = state.arm_slot(
        contact_key=key, kind="checkin", interest_id=None,
        anchor_at=NOW - 100, planned_at=NOW - 100, fire_at=NOW - 1,
        payload={},
    )
    claim = state.claim_due(now=NOW, lease_seconds=900)
    assert claim is not None and claim.slot_id == slot_id
    result = state.register_inbound(
        profile="poke", contact_id="contact-a", route=ROUTE,
        timezone_name="America/New_York", source_id="new", received_at=NOW + 1,
    )
    assert result["cancelled"] == 1
    assert not state.claim_is_current(claim)
    assert state.slot(slot_id)["status"] == "cancelled"


def test_claims_are_overlap_safe_restart_safe_and_stale_slots_die(tmp_path: Path):
    first = ProactiveStateStore(tmp_path / "state.db")
    key = register_messages(first)
    first.arm_slot(
        contact_key=key, kind="checkin", interest_id=None,
        anchor_at=NOW - 100, planned_at=NOW - 100, fire_at=NOW - 1, payload={},
    )
    other = ProactiveStateStore(tmp_path / "state.db")
    claim = first.claim_due(now=NOW, lease_seconds=10)
    assert claim is not None
    assert other.claim_due(now=NOW, lease_seconds=10) is None
    recovered = other.claim_due(now=NOW + 11, lease_seconds=10)
    assert recovered is not None and recovered.slot_id == claim.slot_id
    assert other.finish_claim(recovered, now=NOW + 11)

    stale_id = first.arm_slot(
        contact_key=key, kind="interest_share", interest_id="cars",
        anchor_at=NOW - 200_000, planned_at=NOW - 200_000,
        fire_at=NOW - 24 * 3600 - 1, payload={"topic": "cars"},
    )
    other.claim_due(now=NOW, lease_seconds=10)
    assert other.slot(stale_id)["status"] == "cancelled"
    assert other.slot(stale_id)["status_reason"] == "stale"


def test_dual_profile_conflict_refuses_across_explicit_state_dbs(tmp_path: Path):
    poke_home = tmp_path / "profiles" / "poke"
    guest_home = tmp_path / "profiles" / "guest"
    poke = ProactiveScheduler(
        state_db_path=poke_home / "state.db", profile_home=poke_home,
        profile_name="poke", config=config(),
    )
    poke.note_inbound(contact_route(), message_id="poke-1", received_at=NOW)
    guest = ProactiveScheduler(
        state_db_path=guest_home / "state.db", profile_home=guest_home,
        profile_name="guest", config=config(),
    )
    with pytest.raises(RuntimeError, match="ownership conflict"):
        guest.note_inbound(
            contact_route(profile="guest"), message_id="guest-1", received_at=NOW + 1,
        )
    with pytest.raises(RuntimeError, match="owned by both"):
        assert_no_profile_conflicts({"poke": ["contact-a"], "guest": ["contact-a"]})


def test_cross_profile_registry_serializes_overlapping_first_claim(tmp_path: Path):
    homes = {name: tmp_path / "profiles" / name for name in ("poke", "guest")}
    schedulers = {
        name: ProactiveScheduler(
            state_db_path=home / "state.db", profile_home=home,
            profile_name=name, config=config(),
        )
        for name, home in homes.items()
    }

    def claim(name: str) -> str:
        schedulers[name].note_inbound(
            contact_route(profile=name), message_id=f"{name}-first", received_at=NOW,
        )
        return name

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, name) for name in ("poke", "guest")]
    successes = [future.result() for future in futures if future.exception() is None]
    failures = [future.exception() for future in futures if future.exception() is not None]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "ownership conflict" in str(failures[0])


def test_dm_and_bluebubbles_only(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="DM-only"):
        state.register_inbound(
            profile="poke", contact_id="c", route={**ROUTE, "chat_type": "group"},
            timezone_name="UTC", source_id="x", received_at=NOW,
        )
    with pytest.raises(ValueError, match="bluebubbles-only"):
        state.register_inbound(
            profile="poke", contact_id="c", route={**ROUTE, "platform": "telegram"},
            timezone_name="UTC", source_id="x", received_at=NOW,
        )


def test_one_strike_rejects_delayed_pre_send_inbound_and_newer_inbound_clears(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=NOW - 50 * 3600)
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")
    make_interest(store)
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(weekly_interest_cap=3), profile="poke",
    )
    send_at = NOW - 49 * 3600
    sent(store, "unanswered", when=send_at)
    scheduler._import_ledger_actions(contact_route())
    assert scheduler.eligibility_reason(state.contact_key("contact-a"), "interest_share", now=NOW) == "one_strike_unanswered"

    # Arrives late at ingress, but was received before the proactive action.
    scheduler.note_inbound(
        contact_route(), message_id="delayed", received_at=send_at - 10,
    )
    assert scheduler.eligibility_reason(state.contact_key("contact-a"), "interest_share", now=NOW) == "one_strike_unanswered"

    scheduler.note_inbound(
        contact_route(), message_id="after", received_at=send_at + 10,
    )
    assert scheduler.eligibility_reason(state.contact_key("contact-a"), "interest_share", now=NOW) is None

    sent(store, "recent", when=NOW - 3600, outcome=ProactiveOutcome.ENGAGED)
    scheduler._import_ledger_actions(contact_route())
    assert scheduler.eligibility_reason(state.contact_key("contact-a"), "interest_share", now=NOW) == "minimum_gap"


def test_three_consecutive_ignored_or_dismissed_back_off_thirty_days(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=NOW - 100)
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")
    make_interest(store)
    for index, outcome in enumerate((
        ProactiveOutcome.IGNORED,
        ProactiveOutcome.DISMISSED,
        ProactiveOutcome.IGNORED,
    )):
        sent(store, f"s-{index}", when=NOW - (index + 3) * 86_400, outcome=outcome)
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    scheduler._import_ledger_actions(contact_route())
    key = state.contact_key("contact-a")
    assert scheduler.eligibility_reason(key, "interest_share", now=NOW) == "backoff_active"
    first_until = scheduler.get_contact(key)["disabled_until"]
    assert first_until is not None

    later = float(first_until) + 1
    for index in range(5):
        scheduler.note_inbound(
            contact_route(), message_id=f"fresh-{index}", received_at=later - 10 + index,
        )
    assert scheduler.eligibility_reason(key, "interest_share", now=later) is None
    assert scheduler.get_contact(key)["disabled_until"] == first_until


def test_serious_register_arms_only_checkin_and_dry_run_never_sends(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=NOW - 4 * 3600 - 10, serious=True)
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")
    make_interest(store)
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    result = scheduler.tick(now=NOW)
    assert result["armed"] == 1
    with scheduler._connect() as con:
        rows = con.execute("SELECT kind,status FROM proactive_slot").fetchall()
    assert [(row["kind"], row["status"]) for row in rows] == [("checkin", "armed")]
    assert store.recent_proactive_sends() == []


def test_500_character_checkin_ticks_with_compact_audit_and_initiates(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, count=4, start=NOW - 5 * 3600)
    reason = "r" * 500
    state.register_inbound(
        profile="poke", contact_id="contact-a", route=ROUTE,
        timezone_name="America/New_York", source_id="max-reason",
        received_at=NOW - 4 * 3600, checkin_kind="open_loop", checkin_reason=reason,
    )
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    assert scheduler.tick(now=NOW)["armed"] == 1
    with scheduler._connect() as con:
        slot = con.execute("SELECT slot_id,fire_at FROM proactive_slot").fetchone()
    initiated = []
    result = scheduler.tick(
        now=float(slot["fire_at"]) + 1,
        on_dry_run=lambda _route, claim: initiated.append(claim),
    )
    assert result["fired"] == 1
    assert initiated[0].payload["reason"] == reason
    record = ContactMemoryStore(
        tmp_path / "contact-memory", "contact-a"
    ).get_proactive_send(str(slot["slot_id"]))
    assert record is not None
    audit = json.loads(record.candidate_json)
    assert len(record.candidate_json) <= 500
    assert audit == {
        "audit": "checkin", "dry_run": True, "kind": "open_loop",
        "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
    }
    assert reason not in record.candidate_json
    assert scheduler.get_slot(str(slot["slot_id"]))["status"] == "fired"


@pytest.mark.parametrize("failure_edge", ("projection", "initiation"))
def test_checkin_projection_or_initiation_failure_is_immediately_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_edge: str
):
    state = ProactiveStateStore(tmp_path / "state.db")
    key = register_messages(state)
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    slot_id = scheduler.arm_slot(
        contact_route(), kind="checkin", fire_at=NOW - 1,
        payload={"kind": "open_loop", "reason": "follow up"}, now=NOW - 2,
    )
    original_projection = scheduler._project_action_to_ledger

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("projection failed")

    def fail_initiation(_route, _claim):
        raise RuntimeError("initiation failed")

    callback = fail_initiation if failure_edge == "initiation" else None
    if failure_edge == "projection":
        monkeypatch.setattr(scheduler, "_project_action_to_ledger", fail_projection)
    assert scheduler.tick(now=NOW, on_dry_run=callback)["fired"] == 0
    assert scheduler.get_slot(slot_id)["status"] == "armed"
    with scheduler._connect() as con:
        assert con.execute(
            "SELECT 1 FROM proactive_action WHERE action_id=?", (slot_id,)
        ).fetchone() is None

    monkeypatch.setattr(scheduler, "_project_action_to_ledger", original_projection)
    initiated = []
    assert scheduler.tick(
        now=NOW + 1, on_dry_run=lambda _route, claim: initiated.append(claim.slot_id)
    )["fired"] == 1
    assert initiated == [slot_id]
    assert scheduler.get_slot(slot_id)["status"] == "fired"
    assert key == contact_route().contact_hash


def test_open_loop_inbound_arms_contact_local_checkin_not_interest(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=NOW - 3 * 3600)
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")
    make_interest(store)
    state.register_inbound(
        profile="poke", contact_id="contact-a", route=ROUTE,
        timezone_name="America/New_York", source_id="open-loop",
        received_at=NOW - 2 * 3600, checkin_kind="open_loop",
        checkin_reason="wish me luck at my interview",
    )
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    assert scheduler.tick(now=NOW)["armed"] == 1
    with scheduler._connect() as con:
        rows = con.execute("SELECT kind,status FROM proactive_slot").fetchall()
    assert [(row["kind"], row["status"]) for row in rows] == [("checkin", "armed")]
    assert store.recent_proactive_sends() == []


def test_due_interest_share_is_logged_suppressed_not_sent(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    key = register_messages(state)
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")
    make_interest(store)
    slot = state.arm_slot(
        contact_key=key, kind="interest_share", interest_id="cars",
        anchor_at=NOW - 50, planned_at=NOW - 50, fire_at=NOW - 1,
        payload={"topic": "sports cars"},
    )
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    result = scheduler.tick(now=NOW)
    record = store.get_proactive_send(slot)
    assert result["fired"] == 1
    assert record is not None
    assert record.gate_decision is GateDecision.SUPPRESSED
    assert record.sent_at is None
    assert json.loads(record.candidate_json)["dry_run"] is True


def test_outcome_tracking_is_atomic_idempotent_and_updates_bandit(tmp_path: Path):
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")
    make_interest(store)
    outbound = sent(store, "outcome", when=NOW - 60)
    assert classify_inbound_outcome(outbound, "sports cars are getting wild lately honestly") == "engaged"
    first = store.record_proactive_outcome(
        "outcome", "engaged", source_id="inbound:1", now=NOW,
    )
    after_first = store.get_interest("cars")
    second = store.record_proactive_outcome(
        "outcome", "engaged", source_id="inbound:1", now=NOW + 1,
    )
    after_second = store.get_interest("cars")
    assert first.outcome is ProactiveOutcome.ENGAGED
    assert second.outcome_at == first.outcome_at
    assert after_first == after_second
    assert after_first.ts_alpha == 3
    assert after_first.raw_score == pytest.approx(5.2)
    assert store.unfolded_interest_events() == []


def test_inbound_hook_cancels_and_records_next_outcome_within_24h(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    key = register_messages(state)
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")
    make_interest(store)
    sent(store, "prior", when=NOW - 100)
    slot_id = state.arm_slot(
        contact_key=key, kind="checkin", interest_id=None,
        anchor_at=NOW - 10, planned_at=NOW - 10, fire_at=NOW + 100, payload={},
    )
    result = handle_inbound(
        state_db=tmp_path / "state.db",
        contact_memory_root=tmp_path / "contact-memory",
        profile="poke", contact_id="contact-a", route=ROUTE,
        timezone_name="America/New_York", source_id="reply",
        text="sports cars are honestly getting ridiculously fast now",
        received_at=NOW, config=config(),
    )
    assert result["cancelled"] == 1
    assert result["outcome"] == "engaged"
    assert state.slot(slot_id)["status"] == "cancelled"
    assert store.get_proactive_send("prior").outcome is ProactiveOutcome.ENGAGED
    with state._connect() as con:
        action = con.execute(
            "SELECT outcome FROM proactive_action WHERE action_id='prior'"
        ).fetchone()
    assert action["outcome"] == "engaged"

    replay = handle_inbound(
        state_db=tmp_path / "state.db",
        contact_memory_root=tmp_path / "contact-memory",
        profile="poke", contact_id="contact-a", route=ROUTE,
        timezone_name="America/New_York", source_id="reply",
        text="sports cars are honestly getting ridiculously fast now",
        received_at=NOW, config=config(),
    )
    assert replay["inserted"] is False
    assert replay["outcome"] == "engaged"


def test_tick_marks_unanswered_send_ignored_after_24h_once(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=NOW - 3 * 86_400)
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")
    make_interest(store)
    sent(store, "old", when=NOW - 24 * 3600 - 1)
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    assert scheduler.tick(now=NOW)["ignored"] == 1
    assert scheduler.tick(now=NOW + 1)["ignored"] == 0
    assert store.get_proactive_send("old").outcome is ProactiveOutcome.IGNORED
    assert store.get_interest("cars").ts_beta == 2


def test_sibling_exploration_is_blocked_if_one_of_previous_three_explored(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=NOW - 100)
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")
    make_interest(store, "parent", topic="cars")
    make_interest(store, "sports", topic="sports cars", parent_id="parent")
    make_interest(store, "paint", topic="car paint", parent_id="parent")
    sent(
        store, "explored", interest_id="paint", when=NOW - 49 * 3600,
        kind=ProactiveSendKind.EXPLORATION, outcome=ProactiveOutcome.ENGAGED,
    )
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    scheduler._import_ledger_actions(contact_route())
    for index in range(40):
        selected = scheduler.select_topic(
            contact_route(), now=NOW, selection_nonce=f"seed-{index}",
        )
        assert selected is not None
        assert selected.kind is ProactiveSendKind.INTEREST_SHARE


def test_disabled_is_a_hard_stop_for_tick_and_arm(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state)
    make_interest(ContactMemoryStore(tmp_path / "contact-memory", "contact-a"))
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=ProactiveConfig(enabled=False), profile="poke",
    )
    assert scheduler.tick(now=NOW) == {"armed": 0, "fired": 0, "ignored": 0}
    with pytest.raises(RuntimeError, match="disabled"):
        scheduler.arm_slot(
            contact_route(), kind="interest_share", fire_at=NOW + 1, now=NOW,
        )
    with scheduler._connect() as con:
        assert con.execute("SELECT count(*) FROM proactive_slot").fetchone()[0] == 0


def test_interest_slot_is_pushed_into_contact_local_active_hours(tmp_path: Path):
    zone = ZoneInfo("America/New_York")
    late = datetime.fromtimestamp(NOW, zone).replace(hour=23, minute=0, second=0).timestamp()
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=late - 100)
    make_interest(ContactMemoryStore(tmp_path / "contact-memory", "contact-a"))
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    assert scheduler.tick(now=late)["armed"] == 1
    with scheduler._connect() as con:
        fire_at = float(con.execute("SELECT fire_at FROM proactive_slot").fetchone()[0])
    local = datetime.fromtimestamp(fire_at, zone)
    assert (local.hour, local.minute) >= (9, 0)
    assert (local.hour, local.minute) <= (9, 45)


def test_overlapping_ticks_atomically_arm_one_slot(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state)
    make_interest(ContactMemoryStore(tmp_path / "contact-memory", "contact-a"))
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: scheduler.tick(now=NOW), range(8)))
    with scheduler._connect() as con:
        live = con.execute(
            "SELECT count(*) FROM proactive_slot WHERE status IN ('armed','claimed')"
        ).fetchone()[0]
    assert live == 1


def test_multitick_dry_runs_enforce_one_strike_min_gap_and_weekly_cap(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state)
    make_interest(ContactMemoryStore(tmp_path / "contact-memory", "contact-a"))
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(weekly_interest_cap=2, weekly_total_cap=3), profile="poke",
    )

    assert scheduler.tick(now=NOW)["armed"] == 1
    with scheduler._connect() as con:
        first_fire = float(con.execute("SELECT fire_at FROM proactive_slot").fetchone()[0])
    assert scheduler.tick(now=first_fire + 1)["fired"] == 1
    # Many ticks cannot renew work while the dry-run action is unanswered.
    for step in range(1, 40):
        scheduler.tick(now=first_fire + 1 + step * 1800)
    with scheduler._connect() as con:
        assert con.execute("SELECT count(*) FROM proactive_action WHERE status='dry_run'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM proactive_slot WHERE status IN ('armed','claimed')").fetchone()[0] == 0

    scheduler.note_inbound(
        contact_route(), message_id="reply-one", received_at=first_fire + 2,
    )
    second_plan = first_fire + 49 * 3600
    assert scheduler.tick(now=second_plan)["armed"] == 1
    with scheduler._connect() as con:
        second_fire = float(con.execute(
            "SELECT fire_at FROM proactive_slot WHERE status='armed'"
        ).fetchone()[0])
    assert scheduler.tick(now=second_fire + 1)["fired"] == 1
    scheduler.note_inbound(
        contact_route(), message_id="reply-two", received_at=second_fire + 2,
    )
    assert scheduler.tick(now=second_fire + 49 * 3600)["armed"] == 0
    with scheduler._connect() as con:
        assert con.execute("SELECT count(*) FROM proactive_action WHERE status='dry_run'").fetchone()[0] == 2


def test_delayed_pre_slot_inbound_does_not_cancel_or_invalidate_later_work(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=NOW - 1000)
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    slot_id = scheduler.arm_slot(
        contact_route(), kind="checkin", fire_at=NOW + 10, now=NOW,
    )
    result = scheduler.note_inbound(
        contact_route(), message_id="late-delivery", received_at=NOW - 1,
    )
    assert result["cancelled"] == 0
    assert scheduler.get_slot(slot_id)["status"] == "armed"
    claims = scheduler.claim_due(worker_id="test", now=NOW + 11)
    assert [claim.slot_id for claim in claims] == [slot_id]


@pytest.mark.asyncio
async def test_async_inbound_persists_canonical_contact_id_and_tick_survives(tmp_path: Path):
    route = contact_route()
    for index in range(5):
        await handle_inbound_async(
            profile_home=tmp_path,
            profile_name="poke",
            proactive_config={"agent": {"proactive": {"enabled": True}}},
            route=route,
            message_id=f"async-{index}",
            text="ordinary message",
            received_at=NOW - 10 + index,
        )
    scheduler = ProactiveScheduler(
        state_db_path=tmp_path / "state.db", profile_home=tmp_path,
        profile_name="poke", config=config(),
    )
    contact = scheduler.get_contact(route.contact_hash)
    assert contact["contact_id"] == "contact-a"
    assert scheduler.tick(now=NOW)["armed"] == 0
