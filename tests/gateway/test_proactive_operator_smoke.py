from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gateway.contact_memory.schema import Interest, InterestState, InterestValence
from gateway.contact_memory.store import ContactMemoryStore
from gateway.proactive_scheduler import ContactRoute, ProactiveConfig, ProactiveScheduler
from scripts.proactive_operator_smoke import arm_operator_smoke

NOW = 1_800_000_000.0
ROUTE = ContactRoute(
    contact_id="kosta-owner",
    profile_name="poke",
    timezone="America/New_York",
    principal="owner",
    chat_type="dm",
    chat_id="iMessage;-;existing",
    user_id="owner",
    session_id="parent",
)


def _config() -> dict:
    return {
        "agent": {
            "proactive": {
                "enabled": True,
                "mode": "observe",
                "transport_owner_profile": "poke",
                "allowed_contacts": [
                    {"profile": "poke", "contact_id": "kosta-owner", "principal": "owner"},
                    {"profile": "guest", "contact_id": "stephen-lucier", "principal": "guest"},
                ],
            }
        }
    }


def _interest(home: Path) -> None:
    ContactMemoryStore(home / "contact-memory", "kosta-owner").put_interest(Interest(
        interest_id="cars", topic="sports cars", parent_id=None, raw_score=4.0,
        last_evidence_at=NOW, evidence_count=4, valence=InterestValence.POSITIVE,
        half_life_days=90, state=InterestState.ACTIVE, ts_alpha=2, ts_beta=1,
        created_at=NOW - 10, updated_at=NOW, retired_at=None,
    ))


def test_command_arms_tagged_observe_slot_for_bound_exact_dm(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(_config()), encoding="utf-8")
    scheduler = ProactiveScheduler(
        state_db_path=tmp_path / "state.db",
        profile_home=tmp_path,
        profile_name="poke",
        config=ProactiveConfig.from_mapping(_config()),
    )
    for index in range(5):
        scheduler.note_inbound(ROUTE, message_id=f"m-{index}", received_at=NOW - 10 + index)
    _interest(tmp_path)
    assert scheduler.bind_route_fingerprint(ROUTE, "existing-dm-fingerprint")

    result = arm_operator_smoke(
        profile_home=tmp_path,
        profile="poke",
        contact_id="kosta-owner",
        confirmed=True,
        now=NOW,
    )

    assert result["armed"] is True
    assert result["mode"] == "observe"
    assert result["operator_smoke"] is True
    slot = scheduler.get_slot(result["slot_id"])
    assert slot is not None
    assert slot["status"] == "armed"
    assert slot["reason"] == "operator_smoke"


def test_command_refuses_wrong_contact_and_missing_confirmation(tmp_path: Path):
    with pytest.raises(ValueError, match="exact fixed contact"):
        arm_operator_smoke(
            profile_home=tmp_path,
            profile="poke",
            contact_id="stephen-lucier",
            confirmed=True,
        )
    with pytest.raises(PermissionError, match="confirm-arm"):
        arm_operator_smoke(
            profile_home=tmp_path,
            profile="poke",
            contact_id="kosta-owner",
            confirmed=False,
        )
