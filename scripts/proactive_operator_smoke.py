#!/usr/bin/env python3
"""Arm one exact-route proactive operator smoke without sending directly."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.proactive_scheduler import (
    ProactiveConfig,
    ProactiveScheduler,
    ProactiveStateStore,
    request_proactive_wake,
)

_EXACT_CONTACT = {"poke": "kosta-owner"}


def _operator_candidate(now: float) -> dict[str, Any]:
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    return {
        "topic": "Hermes proactive transport verification",
        "concrete_item": f"Hermes proactive delivery transport smoke {stamp}",
        "why_now": "The operator requested an immediate end-to-end delivery verification.",
        "source_url": "https://hermes-agent.nousresearch.com/docs",
        "freshness_ts": now,
    }


def arm_operator_smoke(
    *,
    profile_home: str | Path,
    profile: str,
    contact_id: str,
    confirmed: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Create one immediate tagged slot for the profile's fixed existing DM."""
    resolved_profile = str(profile).strip()
    expected_contact = _EXACT_CONTACT.get(resolved_profile)
    if expected_contact is None or contact_id != expected_contact:
        raise ValueError("operator smoke requires the profile's exact fixed contact")
    if not confirmed:
        raise PermissionError("explicit --confirm-arm is required")
    home = Path(profile_home).expanduser().resolve()
    config_path = home / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("profile config must be a mapping")
    config = ProactiveConfig.from_mapping(raw)
    scheduler = ProactiveScheduler(
        state_db_path=home / "state.db",
        profile_home=home,
        profile_name=resolved_profile,
        config=config,
    )
    contacts = {
        contact.contact_id: contact
        for contact in ProactiveStateStore(home / "state.db").contacts()
    }
    contact = contacts.get(contact_id)
    if contact is None:
        raise ValueError("exact contact is not registered in proactive state")
    route = scheduler.route_for_contact(contact)
    state = scheduler.get_contact(route.contact_hash)
    if state is None:
        raise ValueError("exact existing DM route is not registered")
    commitment = scheduler.operator_route_commitment(route.as_dict())
    timestamp = time.time() if now is None else float(now)
    slot_id = scheduler.arm_operator_smoke(
        route,
        route_commitment=commitment,
        candidate_override=_operator_candidate(timestamp),
        replace_armed_slot=True,
        now=timestamp,
    )
    request_proactive_wake(home, slot_id=slot_id)
    return {
        "armed": True,
        "slot_id": slot_id,
        "profile": resolved_profile,
        "contact_id": contact_id,
        "mode": config.mode.value,
        "operator_smoke": True,
    }


def operator_smoke_status(*, profile_home: str | Path, slot_id: str) -> dict[str, Any]:
    home = Path(profile_home).expanduser().resolve()
    row = None
    for attempt in range(5):
        try:
            with sqlite3.connect(home / "state.db", timeout=5) as con:
                con.row_factory = sqlite3.Row
                row = con.execute(
                    """SELECT s.status slot_status,s.reason slot_reason,
                              d.state delivery_state,d.projection_state,d.transport_message_id,
                              d.last_error_class
                       FROM proactive_slot s LEFT JOIN proactive_delivery d USING(slot_id)
                       WHERE s.slot_id=?""",
                    (slot_id,),
                ).fetchone()
            break
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))
    if row is None:
        raise ValueError("operator smoke slot does not exist")
    result = {key: row[key] for key in row.keys()}
    delivery_state = str(result.get("delivery_state") or "")
    projection_state = str(result.get("projection_state") or "")
    result["success"] = delivery_state == "sent" and projection_state == "complete"
    result["terminal"] = bool(
        result["success"]
        or delivery_state in {
            "suppressed", "delivery_unknown", "partial_delivery", "failed",
        }
        or result["slot_status"] in {"cancelled", "suppressed"}
        or (delivery_state == "sent" and projection_state == "failed")
    )
    return result


def wait_for_operator_smoke(
    *,
    profile_home: str | Path,
    slot_id: str,
    timeout_seconds: float = 180.0,
    poll_seconds: float = 1.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = operator_smoke_status(profile_home=profile_home, slot_id=slot_id)
        if result["terminal"]:
            return result
        if time.monotonic() >= deadline:
            return {**result, "terminal": True, "success": False, "timed_out": True}
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(_EXACT_CONTACT), required=True)
    parser.add_argument("--profile-home", required=True)
    parser.add_argument("--contact-id", required=True)
    parser.add_argument("--confirm-arm", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)
    result = arm_operator_smoke(
        profile_home=args.profile_home,
        profile=args.profile,
        contact_id=args.contact_id,
        confirmed=args.confirm_arm,
    )
    terminal = wait_for_operator_smoke(
        profile_home=args.profile_home,
        slot_id=result["slot_id"],
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps({**result, **terminal}, sort_keys=True))
    return 0 if terminal["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
