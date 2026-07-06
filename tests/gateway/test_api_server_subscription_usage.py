import asyncio
import json
from unittest.mock import MagicMock

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _make_adapter():
    return APIServerAdapter(PlatformConfig(enabled=True, token="test-key"))


def test_redact_usage_error_hides_auth_material():
    msg = 'Authorization: Bearer abc.def.ghi cookie=session=secret other text'
    redacted = APIServerAdapter._redact_usage_error(msg)
    assert 'abc.def.ghi' not in redacted
    assert 'session=secret' not in redacted
    assert '[redacted]' in redacted


def test_codexbar_usage_payload_parses_provider_json(monkeypatch, tmp_path):
    adapter = _make_adapter()
    fake_cli = tmp_path / "codexbar"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("HERMES_CODEXBAR_CLI", str(fake_cli))

    payload = json.dumps([
        {"provider": "claude", "usage": {"primary": {"usedPercent": 2}}},
        {"provider": "codex", "error": {"message": "token_expired"}},
    ]).encode()

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return payload, b""

    async def fake_create(*args, **kwargs):
        assert args[:3] == (str(fake_cli), "usage", "--format")
        assert "--no-color" in args
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    result = asyncio.run(adapter._codexbar_usage_payload("all"))

    assert result["ok"] is True
    assert result["okCount"] == 1
    assert result["errorCount"] == 1
    assert result["providers"][0]["provider"] == "claude"


def test_codexbar_usage_payload_forces_claude_oauth_source(monkeypatch, tmp_path):
    adapter = _make_adapter()
    fake_cli = tmp_path / "codexbar"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("HERMES_CODEXBAR_CLI", str(fake_cli))

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b'[{"provider":"claude","source":"oauth","usage":{"primary":{"usedPercent":2}}}]', b""

    captured = {}

    async def fake_create(*args, **kwargs):
        captured["args"] = args
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    result = asyncio.run(adapter._codexbar_usage_payload("claude"))

    assert result["ok"] is True
    assert "--provider" in captured["args"]
    assert "claude" in captured["args"]
    assert "--source" in captured["args"]
    assert "oauth" in captured["args"]


def test_handle_subscription_usage_returns_payload(monkeypatch):
    adapter = _make_adapter()
    request = MagicMock()
    request.headers = {}
    request.query = {"provider": "claude"}
    monkeypatch.setattr(adapter, "_check_auth", lambda request: None)

    async def fake_payload(provider):
        return {"ok": True, "provider": provider, "providers": []}

    monkeypatch.setattr(adapter, "_codexbar_usage_payload", fake_payload)

    response = asyncio.run(adapter._handle_subscription_usage(request))
    assert response.status == 200
    assert json.loads(response.text)["provider"] == "claude"


def test_codexbar_enabled_aggregates_configured_providers(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setattr(adapter, "_enabled_codexbar_providers", lambda: ["codex", "claude"])

    async def fake_payload(provider):
        return {
            "ok": True,
            "provider": provider,
            "okCount": 1,
            "errorCount": 0,
            "providers": [{"provider": provider, "usage": {"primary": {"usedPercent": 1}}}],
        }

    async def fail_create(*args, **kwargs):  # pragma: no cover - proves no monolithic CLI sweep
        raise AssertionError("enabled aggregation should query providers individually")

    monkeypatch.setattr(adapter, "_codexbar_usage_payload", fake_payload)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_create)
    result = asyncio.run(APIServerAdapter._codexbar_usage_payload(adapter, "enabled"))

    assert result["ok"] is True
    assert result["provider"] == "enabled"
    assert result["okCount"] == 2
    assert [item["provider"] for item in result["providers"]] == ["codex", "claude"]
