from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup_update_service.py"
SPEC = importlib.util.spec_from_file_location("setup_update_service", SCRIPT)
assert SPEC and SPEC.loader
SETUP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SETUP
SPEC.loader.exec_module(SETUP)


def test_update_service_builds_with_the_local_notary_profile(tmp_path: Path) -> None:
    payload = SETUP.plist_payload(
        "/usr/bin/python3",
        tmp_path / "service.py",
        tmp_path / "repo",
        tmp_path / "state",
    )

    assert payload["EnvironmentVariables"] == {
        "APPLE_NOTARY_PROFILE": "my-notary-profile",
    }
