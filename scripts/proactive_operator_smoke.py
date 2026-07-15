#!/usr/bin/env python3
"""Arm one exact-route proactive operator smoke without sending directly."""
from __future__ import annotations

import argparse
import json
import sys
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
)

_EXACT_CONTACT = {"poke": "kosta-owner", "guest": "stephen-lucier"}


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
    slot_id = scheduler.arm_operator_smoke(
        route,
        route_commitment=commitment,
        now=now,
    )
    return {
        "armed": True,
        "slot_id": slot_id,
        "profile": resolved_profile,
        "contact_id": contact_id,
        "mode": config.mode.value,
        "operator_smoke": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(_EXACT_CONTACT), required=True)
    parser.add_argument("--profile-home", required=True)
    parser.add_argument("--contact-id", required=True)
    parser.add_argument("--confirm-arm", action="store_true")
    args = parser.parse_args(argv)
    result = arm_operator_smoke(
        profile_home=args.profile_home,
        profile=args.profile,
        contact_id=args.contact_id,
        confirmed=args.confirm_arm,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
