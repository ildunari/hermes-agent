from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.run import (
    ProactiveTurnRequest,
    _run_proactive_tick_once,
    run_proactive_child_turn,
)
from gateway.proactive_checkin import assistant_first_parent_history
from gateway.proactive_scheduler import ContactRoute, ProactiveConfig, ProactiveScheduler
from hermes_state import SessionDB


def parent(db: SessionDB, *, chat_type: str = "dm") -> None:
    db.create_session(
        "parent",
        "bluebubbles",
        user_id="contact-a",
        session_key="agent:main:bluebubbles:dm:contact-a",
        chat_id="iMessage;-;+15555550123",
        chat_type=chat_type,
        model="test-model",
        model_config={"temperature": 0.2},
        system_prompt="stable-system-prefix",
    )
    db.append_message("parent", "user", "rough day")
    db.append_message("parent", "assistant", "i'm around")


def test_proactive_entry_point_creates_assistant_first_child_with_lineage(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    parent(db)
    captured: list[ProactiveTurnRequest] = []

    def generate(request: ProactiveTurnRequest) -> str:
        captured.append(request)
        return "how you holding up"

    result = run_proactive_child_turn(
        session_db=db,
        parent_session_id="parent",
        purpose_prompt='<checkin_texture private="true">tiny follow-up</checkin_texture>',
        kind="checkin",
        generate=generate,
        child_session_id="child",
        now_ts=1_800_000_000.0,
    )

    assert result["session_id"] == "child"
    assert result["parent_session_id"] == "parent"
    assert [row["role"] for row in result["messages"]] == ["assistant"]
    assert result["messages"][0]["content"] == "how you holding up"
    assert [row["role"] for row in db.get_messages("parent")] == ["user", "assistant"]

    request = captured[0]
    assert request.cache_system_prompt == "stable-system-prefix"
    assert request.execution_system_prompt.startswith("stable-system-prefix\n\n")
    assert "checkin_texture" in request.execution_system_prompt
    assert [row["role"] for row in request.parent_history] == ["user", "assistant"]
    assert [row["role"] for row in request.generation_history] == ["user"]

    child = db.get_session("child")
    assert child["parent_session_id"] == "parent"
    assert child["system_prompt"] == db.get_session("parent")["system_prompt"]
    marker = json.loads(child["model_config"])
    assert marker["_proactive_from"] == "parent"
    assert marker["_proactive_kind"] == "checkin"
    assert child["session_key"] is None

    routed = db.find_latest_gateway_session_for_peer(
        source="bluebubbles",
        user_id="contact-a",
        session_key="agent:main:bluebubbles:dm:contact-a",
        chat_id="iMessage;-;+155****0123",
        chat_type="dm",
    )
    assert routed["id"] == "parent"


def test_proactive_entry_point_never_injects_a_synthetic_user_turn(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    parent(db)
    seen = {}

    def generate(request: ProactiveTurnRequest):
        seen["history"] = request.parent_history
        return {"final_response": "verdict?"}

    run_proactive_child_turn(
        session_db=db,
        parent_session_id="parent",
        purpose_prompt="execution only",
        kind="checkin",
        generate=generate,
        child_session_id="child",
    )
    child_messages = db.get_messages("child")
    assert [row["role"] for row in child_messages] == ["assistant"]
    assert all(row["content"] != "execution only" for row in child_messages)
    assert len(seen["history"]) == 2


def test_generation_failure_does_not_leave_empty_child(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    parent(db)
    with pytest.raises(ValueError, match="no assistant content"):
        run_proactive_child_turn(
            session_db=db,
            parent_session_id="parent",
            purpose_prompt="execution only",
            kind="checkin",
            generate=lambda request: "",
            child_session_id="child",
        )
    assert db.get_session("child") is None


def test_initiated_turn_refuses_group_parent(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    parent(db, chat_type="group")
    with pytest.raises(ValueError, match="DM-only"):
        run_proactive_child_turn(
            session_db=db,
            parent_session_id="parent",
            purpose_prompt="execution only",
            kind="checkin",
            generate=lambda request: "hello",
            child_session_id="child",
        )
    assert db.get_session("child") is None


def test_atomic_session_api_rejects_duplicate_child_and_preserves_first_turn(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    parent(db)
    db.create_initiated_assistant_child(
        parent_session_id="parent",
        child_session_id="child",
        assistant_content="first",
        initiated_kind="checkin",
        timestamp=1_800_000_000.0,
    )
    with pytest.raises(ValueError, match="already exists"):
        db.create_initiated_assistant_child(
            parent_session_id="parent",
            child_session_id="child",
            assistant_content="second",
            initiated_kind="checkin",
        )
    rows = db.get_messages("child")
    assert [(row["role"], row["content"]) for row in rows] == [("assistant", "first")]


def test_parent_history_repairs_adjacent_roles_without_synthetic_rows():
    repaired = assistant_first_parent_history([
        {"role": "assistant", "content": "orphan"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "stale-user"},
        {"role": "user", "content": "latest-user"},
        {"role": "assistant", "content": "tail excluded"},
    ])
    assert [(row["role"], row["content"]) for row in repaired] == [
        ("user", "u1"), ("assistant", "a1"), ("user", "latest-user"),
    ]


def test_real_tick_wires_due_checkin_to_dry_run_child_without_peer_hijack(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    parent(db)
    allowlist=(('poke','kosta-owner','owner'),('guest','stephen-lucier','guest'))
    cfg = ProactiveConfig(enabled=True,allowed_contacts=allowlist)
    scheduler = ProactiveScheduler(
        state_db_path=tmp_path / "state.db", profile_home=tmp_path,
        profile_name="poke", config=cfg,
    )
    route = ContactRoute(
        contact_id="kosta-owner", profile_name="poke", timezone="America/New_York",
        chat_type="dm", chat_id="iMessage;-;+155****0123", user_id="contact-a",
        session_id="parent",
    )
    for index in range(5):
        scheduler.note_inbound(
            route, message_id=f"m-{index}", received_at=1_800_000_000.0 - 100 + index,
        )
    slot_id = scheduler.arm_slot(
        route, kind="checkin", fire_at=1_800_000_001.0,
        payload={"kind": "open_loop", "reason": "the interview", "session_id": "parent"},
        now=1_800_000_000.0,
    )

    seen = []
    result = _run_proactive_tick_once(
        profile_home=tmp_path,
        profile="poke",
        config_raw={"agent": {"proactive": {"enabled": True,"mode":"observe","allowed_contacts":[
            {"profile":"poke","contact_id":"kosta-owner","principal":"owner"},
            {"profile":"guest","contact_id":"stephen-lucier","principal":"guest"},
        ]}}},
        session_db=db,
        generate=lambda request: seen.append(request) or "how'd the interview go",
        gate_verdict=lambda _request: {"allow": True, "reason": "welcome"},
        now=1_800_000_002.0,
    )
    assert result["fired"] == 1
    assert result["initiated"] == 1
    assert len(seen) == 1
    assert db.get_messages(f"proactive-{slot_id}")[0]["role"] == "assistant"
    with scheduler._connect() as con:
        action = con.execute(
            "SELECT status,sent_at FROM proactive_action WHERE action_id=?", (slot_id,)
        ).fetchone()
    assert action["status"] == "dry_run"
    assert action["sent_at"] is None
    routed = db.find_latest_gateway_session_for_peer(
        source="bluebubbles", user_id="contact-a",
        session_key="agent:main:bluebubbles:dm:contact-a",
        chat_id="iMessage;-;+155****0123", chat_type="dm",
    )
    assert routed["id"] == "parent"


@pytest.mark.parametrize(
    ("gate_verdict", "expected_reason"),
    (
        (None, "final_gate_unavailable"),
        (lambda _request: {"allow": False, "reason": "not now"}, "model_gate:not now"),
    ),
)
def test_real_tick_suppresses_checkin_when_strict_gate_rejects_or_is_unavailable(
    tmp_path: Path,
    gate_verdict,
    expected_reason: str,
):
    db = SessionDB(tmp_path / "state.db")
    parent(db)
    cfg = ProactiveConfig(
        enabled=True,
        allowed_contacts=(
            ("poke", "kosta-owner", "owner"),
            ("guest", "stephen-lucier", "guest"),
        ),
    )
    scheduler = ProactiveScheduler(
        state_db_path=tmp_path / "state.db",
        profile_home=tmp_path,
        profile_name="poke",
        config=cfg,
    )
    route = ContactRoute(
        contact_id="kosta-owner",
        profile_name="poke",
        timezone="America/New_York",
        chat_type="dm",
        chat_id="iMessage;-;+155****0123",
        user_id="contact-a",
        session_id="parent",
    )
    for index in range(5):
        scheduler.note_inbound(
            route,
            message_id=f"m-{index}",
            received_at=1_800_000_000.0 - 100 + index,
        )
    slot_id = scheduler.arm_slot(
        route,
        kind="checkin",
        fire_at=1_800_000_001.0,
        payload={"kind": "open_loop", "reason": "the interview", "session_id": "parent"},
        now=1_800_000_000.0,
    )
    generated: list[ProactiveTurnRequest] = []

    result = _run_proactive_tick_once(
        profile_home=tmp_path,
        profile="poke",
        config_raw={
            "agent": {
                "proactive": {
                    "enabled": True,
                    "mode": "observe",
                    "allowed_contacts": [
                        {"profile": "poke", "contact_id": "kosta-owner", "principal": "owner"},
                        {
                            "profile": "guest",
                            "contact_id": "stephen-lucier",
                            "principal": "guest",
                        },
                    ],
                },
            },
        },
        session_db=db,
        generate=lambda request: generated.append(request) or "how'd the interview go",
        gate_verdict=gate_verdict,
        now=1_800_000_002.0,
    )

    assert result["fired"] == 0
    assert result["initiated"] == 0
    assert generated == []
    assert scheduler.get_slot(slot_id)["status"] == "suppressed"
    with scheduler._connect() as con:
        action = con.execute(
            "SELECT reason FROM proactive_action WHERE action_id=?",
            (slot_id,),
        ).fetchone()
    assert action["reason"] == expected_reason
