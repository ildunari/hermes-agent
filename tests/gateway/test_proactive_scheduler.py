from __future__ import annotations

import asyncio
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
    values = {
        **ProactiveConfig().__dict__, "enabled": True,
        "allowed_contacts": (
            ("poke", "kosta-owner", "owner"),
            ("guest", "stephen-lucier", "guest"),
        ),
        "alarm_sink_configured": True,
        **overrides,
    }
    cfg = ProactiveConfig(**values)
    cfg.validate()
    return cfg


def contact_route(*, profile: str = "poke", contact_id: str | None = None) -> ContactRoute:
    contact_id = contact_id or ("stephen-lucier" if profile == "guest" else "kosta-owner")
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
    contact_id: str = "kosta-owner",
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


def test_config_modes_default_disabled_and_live_requires_explicit_allowlist():
    cfg = ProactiveConfig.from_mapping({
        "agent": {"proactive": {
            "enabled": True,
            "mode": "live",
            "transport_owner_profile": "poke",
            "allowed_contacts": [
                {"profile": "poke", "contact_id": "kosta-owner", "principal": "owner"},
                {"profile": "guest", "contact_id": "stephen-lucier", "principal": "guest"},
            ],
            "active_hours": {"start": "09:00", "end": "21:30"},
        }}
    })
    assert cfg.enabled is True and cfg.mode.value == "live" and cfg.dry_run is False
    assert ProactiveConfig.from_mapping({"enabled": True}).mode.value == "disabled"
    with pytest.raises(ValueError, match="allowlist"):
        ProactiveConfig.from_mapping({"enabled": True, "mode": "live"})
    with pytest.raises(ValueError, match="allowlist"):
        ProactiveConfig.from_mapping({"enabled": True, "mode": "observe", "allowed_contacts": []})
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
        jitter_key="kosta-owner",
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
        jitter_key="kosta-owner",
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
    make_interest(ContactMemoryStore(memory, "kosta-owner"))
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=memory,
        config=config(), profile="poke",
    )
    assert scheduler.tick(now=NOW)["armed"] == 0
    state.register_inbound(
        profile="poke", contact_id="kosta-owner", route=ROUTE,
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
        profile="poke", contact_id="kosta-owner", route=ROUTE,
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


def test_dual_profile_exact_allowlist_prevents_cross_profile_contact_registration(tmp_path: Path):
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
    guest.note_inbound(contact_route(profile="guest"), message_id="guest-1", received_at=NOW + 1)
    with pytest.raises(ValueError, match="exact proactive allowlist"):
        guest.note_inbound(
            contact_route(profile="guest", contact_id="kosta-owner"),
            message_id="forged", received_at=NOW + 2,
        )
    with pytest.raises(RuntimeError, match="owned by both"):
        assert_no_profile_conflicts({"poke": ["kosta-owner"], "guest": ["kosta-owner"]})


def test_parallel_exact_principals_register_without_cross_profile_collision(tmp_path: Path):
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
        results = list(pool.map(claim, ("poke", "guest")))
    assert sorted(results) == ["guest", "poke"]


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
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
    make_interest(store)
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(weekly_interest_cap=3), profile="poke",
    )
    send_at = NOW - 49 * 3600
    sent(store, "unanswered", when=send_at)
    scheduler._import_ledger_actions(contact_route())
    assert scheduler.eligibility_reason(state.contact_key("kosta-owner"), "interest_share", now=NOW) == "one_strike_unanswered"

    # Arrives late at ingress, but was received before the proactive action.
    scheduler.note_inbound(
        contact_route(), message_id="delayed", received_at=send_at - 10,
    )
    assert scheduler.eligibility_reason(state.contact_key("kosta-owner"), "interest_share", now=NOW) == "one_strike_unanswered"

    scheduler.note_inbound(
        contact_route(), message_id="after", received_at=send_at + 10,
    )
    assert scheduler.eligibility_reason(state.contact_key("kosta-owner"), "interest_share", now=NOW) is None

    sent(store, "recent", when=NOW - 3600, outcome=ProactiveOutcome.ENGAGED)
    scheduler._import_ledger_actions(contact_route())
    assert scheduler.eligibility_reason(state.contact_key("kosta-owner"), "interest_share", now=NOW) == "minimum_gap"


def test_three_consecutive_ignored_or_dismissed_back_off_thirty_days(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=NOW - 100)
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
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
    key = state.contact_key("kosta-owner")
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
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
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


def test_inherited_serious_register_on_neutral_final_message_blocks_interest_for_72h(
    tmp_path: Path,
):
    from gateway.conversation_texture_v2 import _seriousness

    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, count=4, start=NOW - 100)
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
    make_interest(store)

    handle_inbound(
        state_db=tmp_path / "state.db",
        contact_memory_root=tmp_path / "contact-memory",
        profile="poke",
        contact_id="kosta-owner",
        route=ROUTE,
        timezone_name="America/New_York",
        source_id="neutral-tail",
        text="okay",
        received_at=NOW,
        config=config(),
        serious_register=bool(_seriousness("okay", ["my dad died"])),
    )

    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db",
        contact_memory_root=tmp_path / "contact-memory",
        config=config(),
        profile="poke",
    )
    key = state.contact_key("kosta-owner")
    assert scheduler.eligibility_reason(
        key,
        "interest_share",
        now=NOW + 72 * 3600 - 1,
    ) == "serious_mode"


def test_500_character_checkin_ticks_with_compact_audit_and_initiates(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, count=4, start=NOW - 5 * 3600)
    reason = "r" * 500
    state.register_inbound(
        profile="poke", contact_id="kosta-owner", route=ROUTE,
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
        tmp_path / "contact-memory", "kosta-owner"
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
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
    make_interest(store)
    state.register_inbound(
        profile="poke", contact_id="kosta-owner", route=ROUTE,
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
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
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
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
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


@pytest.mark.parametrize("reply", ("ok", "  okay!!!  ", "OK...", "okay?"))
def test_bare_okay_reply_is_neutral_acknowledgement(tmp_path: Path, reply: str):
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
    make_interest(store)
    outbound = sent(store, "outcome", when=NOW - 60)

    assert classify_inbound_outcome(outbound, reply) == "acknowledged"


@pytest.mark.parametrize("reply", ("nah", "nope!", "stop.", "don't care"))
def test_explicit_dismissive_reply_remains_dismissed(tmp_path: Path, reply: str):
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
    make_interest(store)
    outbound = sent(store, "outcome", when=NOW - 60)

    assert classify_inbound_outcome(outbound, reply) == "dismissed"


def test_inbound_hook_cancels_and_records_next_outcome_within_24h(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    key = register_messages(state)
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
    make_interest(store)
    sent(store, "prior", when=NOW - 100)
    slot_id = state.arm_slot(
        contact_key=key, kind="checkin", interest_id=None,
        anchor_at=NOW - 10, planned_at=NOW - 10, fire_at=NOW + 100, payload={},
    )
    result = handle_inbound(
        state_db=tmp_path / "state.db",
        contact_memory_root=tmp_path / "contact-memory",
        profile="poke", contact_id="kosta-owner", route=ROUTE,
        timezone_name="America/New_York", source_id="reply",
        text="sports cars are honestly getting ridiculously fast now",
        received_at=NOW, config=config(),
    )
    assert result["cancelled"] == 1
    assert result["outcome"] == "engaged"
    assert result["outcome_status"] == "provisional"
    assert state.slot(slot_id)["status"] == "cancelled"
    assert store.get_proactive_send("prior").outcome is None
    assert store.get_interest("cars").ts_alpha == 2
    with state._connect() as con:
        action = con.execute(
            "SELECT outcome,outcome_status FROM proactive_action WHERE action_id='prior'"
        ).fetchone()
    assert action["outcome"] == "engaged"
    assert action["outcome_status"] == "provisional"

    replay = handle_inbound(
        state_db=tmp_path / "state.db",
        contact_memory_root=tmp_path / "contact-memory",
        profile="poke", contact_id="kosta-owner", route=ROUTE,
        timezone_name="America/New_York", source_id="reply",
        text="sports cars are honestly getting ridiculously fast now",
        received_at=NOW, config=config(),
    )
    assert replay["inserted"] is False
    assert replay["outcome"] == "engaged"
    assert replay["outcome_status"] == "provisional"


@pytest.mark.asyncio
async def test_provisional_outcome_is_confirmed_and_refined_once(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state)
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
    make_interest(store)
    sent(store, "prior", when=NOW - 100)
    reply = "sports cars are honestly getting ridiculously fast now"
    handle_inbound(
        state_db=tmp_path / "state.db",
        contact_memory_root=tmp_path / "contact-memory",
        profile="poke", contact_id="kosta-owner", route=ROUTE,
        timezone_name="America/New_York", source_id="reply",
        text=reply, received_at=NOW, config=config(),
    )
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )

    async def extractor(**request):
        assert request == {
            "model": "pinned/outcome-v1",
            "text": reply,
            "provisional_outcome": "engaged",
        }
        return {"outcome": "dismissed", "valence": "negative"}

    result = await scheduler.confirm_action_outcome(
        contact_route(), "prior", inbound_text=reply,
        model="pinned/outcome-v1", extractor=extractor,
        source_id="inbound:reply", now=NOW + 1,
    )

    assert result == {
        "outcome": "dismissed", "outcome_status": "confirmed",
        "valence": "negative", "applied": True,
    }
    assert store.get_proactive_send("prior").outcome is ProactiveOutcome.DISMISSED
    interest = store.get_interest("cars")
    assert (interest.ts_alpha, interest.ts_beta) == (2, 2)
    with scheduler._connect() as con:
        action = con.execute(
            "SELECT outcome,outcome_status FROM proactive_action WHERE action_id='prior'"
        ).fetchone()
        audit = dict(con.execute(
            "SELECT * FROM proactive_outcome_confirmation WHERE action_id='prior'"
        ).fetchone())
    assert dict(action) == {"outcome": "dismissed", "outcome_status": "confirmed"}
    assert audit["provisional_outcome"] == "engaged"
    assert audit["confirmed_outcome"] == "dismissed"
    assert audit["confirmed_valence"] == "negative"
    assert audit["model"] == "pinned/outcome-v1"
    assert audit["provisional_at"] == NOW
    assert audit["claimed_at"] == NOW + 1
    assert audit["completed_at"] == NOW + 1
    assert audit["source_id"] == "inbound:reply"
    assert audit["input_chars"] == len(reply)
    assert reply not in json.dumps(audit)
    with pytest.raises(ValueError, match="different outcome"):
        scheduler.set_action_outcome("prior", "engaged", now=NOW + 2)


@pytest.mark.asyncio
async def test_confirmation_claim_is_concurrent_and_replay_idempotent(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state)
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
    make_interest(store)
    sent(store, "prior", when=NOW - 100)
    reply = "sports cars are honestly getting ridiculously fast now"
    handle_inbound(
        state_db=tmp_path / "state.db",
        contact_memory_root=tmp_path / "contact-memory",
        profile="poke", contact_id="kosta-owner", route=ROUTE,
        timezone_name="America/New_York", source_id="reply",
        text=reply, received_at=NOW, config=config(),
    )
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    callback_count = 0

    async def extractor(**request):
        nonlocal callback_count
        callback_count += 1
        started.set()
        await release.wait()
        return {"outcome": "engaged", "valence": "positive"}

    first_call = asyncio.create_task(scheduler.confirm_action_outcome(
        contact_route(), "prior", inbound_text=reply,
        model="pinned/outcome-v1", extractor=extractor,
        source_id="inbound:reply", now=NOW + 1,
    ))
    await started.wait()
    concurrent = await scheduler.confirm_action_outcome(
        contact_route(), "prior", inbound_text=reply,
        model="pinned/outcome-v1", extractor=extractor,
        source_id="inbound:reply", now=NOW + 1,
    )
    release.set()
    first = await first_call
    replay = await scheduler.confirm_action_outcome(
        contact_route(), "prior", inbound_text=reply,
        model="ignored-on-replay", extractor=extractor,
        source_id="inbound:reply", now=NOW + 2,
    )

    assert concurrent["confirmation_status"] == "claimed"
    assert first == {
        "outcome": "engaged", "outcome_status": "confirmed",
        "valence": "positive", "applied": True,
    }
    assert replay == {**first, "applied": False}
    assert callback_count == 1
    interest = store.get_interest("cars")
    assert (interest.ts_alpha, interest.ts_beta) == (3, 1)
    with scheduler._connect() as con:
        assert con.execute(
            "SELECT count(*) FROM proactive_outcome_confirmation WHERE action_id='prior'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "error_code"),
    (("malformed", "malformed_result"), ("unavailable", "callback_unavailable")),
)
async def test_confirmation_failure_is_closed_audited_and_not_retried(
    tmp_path: Path, mode: str, error_code: str,
):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state)
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
    make_interest(store)
    sent(store, "prior", when=NOW - 100)
    inbound_text = "private payload " + "ordinary words " * 500
    handle_inbound(
        state_db=tmp_path / "state.db",
        contact_memory_root=tmp_path / "contact-memory",
        profile="poke", contact_id="kosta-owner", route=ROUTE,
        timezone_name="America/New_York", source_id="reply",
        text=inbound_text, received_at=NOW, config=config(),
    )
    scheduler = ProactiveScheduler(
        state_db=tmp_path / "state.db", contact_memory_root=tmp_path / "contact-memory",
        config=config(), profile="poke",
    )
    calls = 0

    async def extractor(**request):
        nonlocal calls
        calls += 1
        assert len(request["text"]) == 4000
        if mode == "unavailable":
            raise RuntimeError("offline")
        return {"outcome": "engaged", "valence": "negative"}

    first = await scheduler.confirm_action_outcome(
        contact_route(), "prior", inbound_text=inbound_text,
        model="pinned/outcome-v1", extractor=extractor,
        source_id="inbound:reply", now=NOW + 1,
    )
    replay = await scheduler.confirm_action_outcome(
        contact_route(), "prior", inbound_text=inbound_text,
        model="pinned/outcome-v1", extractor=extractor,
        source_id="inbound:reply", now=NOW + 2,
    )

    assert first == replay == {
        "outcome": "engaged", "outcome_status": "provisional",
        "confirmation_status": "failed", "error_code": error_code,
        "applied": False,
    }
    assert calls == 1
    assert store.get_proactive_send("prior").outcome is None
    interest = store.get_interest("cars")
    assert (interest.ts_alpha, interest.ts_beta) == (2, 1)
    with scheduler._connect() as con:
        action = dict(con.execute(
            "SELECT outcome,outcome_status FROM proactive_action WHERE action_id='prior'"
        ).fetchone())
        audit = dict(con.execute(
            "SELECT * FROM proactive_outcome_confirmation WHERE action_id='prior'"
        ).fetchone())
    assert action == {"outcome": "engaged", "outcome_status": "provisional"}
    assert audit["status"] == "failed"
    assert audit["error_code"] == error_code
    assert audit["input_chars"] == 4000
    assert audit["input_sha256"] == hashlib.sha256(inbound_text[:4000].encode()).hexdigest()
    assert inbound_text not in json.dumps(audit)


def test_tick_marks_unanswered_send_ignored_after_24h_once(tmp_path: Path):
    state = ProactiveStateStore(tmp_path / "state.db")
    register_messages(state, start=NOW - 3 * 86_400)
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
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
    store = ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner")
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
    make_interest(ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner"))
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
    make_interest(ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner"))
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
    make_interest(ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner"))
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
    make_interest(ContactMemoryStore(tmp_path / "contact-memory", "kosta-owner"))
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
            proactive_config={"agent": {"proactive": {
                "enabled": True, "mode": "observe",
                "allowed_contacts": [
                    {"profile": "poke", "contact_id": "kosta-owner", "principal": "owner"},
                    {"profile": "guest", "contact_id": "stephen-lucier", "principal": "guest"},
                ],
            }}},
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
    assert contact["contact_id"] == "kosta-owner"
    assert scheduler.tick(now=NOW)["armed"] == 0
