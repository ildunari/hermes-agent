from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_deployed_turn.py"
SPEC = importlib.util.spec_from_file_location("verify_deployed_turn", SCRIPT)
assert SPEC and SPEC.loader
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def test_api_base_url_uses_configured_listener(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("API_SERVER_HOST=100.64.0.10\nAPI_SERVER_PORT=9442\n", encoding="utf-8")
    monkeypatch.setattr(VERIFY, "ENV_CANDIDATES", (env_file,))
    monkeypatch.delenv("API_SERVER_HOST", raising=False)
    monkeypatch.delenv("API_SERVER_PORT", raising=False)

    assert VERIFY.api_base_url() == "http://100.64.0.10:9442"


def test_api_base_url_defaults_to_loopback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(VERIFY, "ENV_CANDIDATES", (tmp_path / "missing",))
    monkeypatch.delenv("API_SERVER_HOST", raising=False)
    monkeypatch.delenv("API_SERVER_PORT", raising=False)

    assert VERIFY.api_base_url() == "http://127.0.0.1:8642"


def test_api_base_url_prefers_global_listener_over_profile_port(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile.env"
    shared = tmp_path / "shared.env"
    profile.write_text("API_SERVER_HOST=100.64.0.10\nAPI_SERVER_PORT=8643\n", encoding="utf-8")
    shared.write_text("API_SERVER_HOST=100.64.0.20\nAPI_SERVER_PORT=8642\n", encoding="utf-8")
    monkeypatch.setattr(VERIFY, "ENV_CANDIDATES", (profile, shared))
    monkeypatch.delenv("API_SERVER_HOST", raising=False)
    monkeypatch.delenv("API_SERVER_PORT", raising=False)

    assert VERIFY.api_base_url() == "http://100.64.0.20:8642"
