import json
import sys
import types

from hermes_cli.fallback_config import codex_home_access_token, get_fallback_chain


def test_codex_home_access_token_reads_codex_cli_auth(tmp_path):
    codex_home = tmp_path / ".codex-photongaming"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "token-123"}}),
        encoding="utf-8",
    )

    assert (
        codex_home_access_token(
            {"provider": "openai-codex", "model": "gpt-5.5", "codex_home": str(codex_home)}
        )
        == "token-123"
    )


def test_codex_home_access_token_refreshes_expiring_codex_cli_auth(tmp_path, monkeypatch):
    codex_home = tmp_path / ".codex-photongaming"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "old-token",
                    "refresh_token": "old-refresh",
                    "account_id": "acct_123",
                },
                "last_refresh": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    fake_auth = types.ModuleType("hermes_cli.auth")
    setattr(fake_auth, "CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS", 120)
    setattr(fake_auth, "_codex_access_token_is_expiring", lambda token, skew: True)

    def fake_refresh(access_token, refresh_token, *, timeout_seconds=20.0):
        assert access_token == "old-token"
        assert refresh_token == "old-refresh"
        return {
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "last_refresh": "2026-07-08T13:00:00Z",
        }

    setattr(fake_auth, "refresh_codex_oauth_pure", fake_refresh)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    assert codex_home_access_token(
        {"provider": "openai-codex", "model": "gpt-5.5", "codex_home": str(codex_home)}
    ) == "new-token"

    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == "new-token"
    assert saved["tokens"]["refresh_token"] == "new-refresh"
    assert saved["tokens"]["account_id"] == "acct_123"
    assert saved["last_refresh"] == "2026-07-08T13:00:00Z"



def test_codex_home_access_token_refuses_expired_token_when_refresh_fails(tmp_path, monkeypatch):
    codex_home = tmp_path / ".codex-photongaming"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "expired-token",
                    "refresh_token": "dead-refresh",
                }
            }
        ),
        encoding="utf-8",
    )

    fake_auth = types.ModuleType("hermes_cli.auth")
    setattr(fake_auth, "CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS", 120)
    setattr(fake_auth, "_codex_access_token_is_expiring", lambda token, skew: True)

    def fake_refresh(access_token, refresh_token, *, timeout_seconds=20.0):
        raise RuntimeError("refresh token reused")

    setattr(fake_auth, "refresh_codex_oauth_pure", fake_refresh)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    assert codex_home_access_token(
        {"provider": "openai-codex", "model": "gpt-5.5", "codex_home": str(codex_home)}
    ) is None



def test_codex_home_access_token_ignores_non_codex_provider(tmp_path):
    codex_home = tmp_path / ".codex-photongaming"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "token-123"}}),
        encoding="utf-8",
    )

    assert codex_home_access_token({"provider": "deepseek", "codex_home": str(codex_home)}) is None


def test_get_fallback_chain_preserves_codex_home():
    chain = get_fallback_chain(
        {
            "fallback_providers": [
                {
                    "provider": "openai-codex",
                    "model": "gpt-5.5",
                    "codex_home": "/Users/Kosta/.codex-photongaming",
                }
            ]
        }
    )

    assert chain[0]["codex_home"] == "/Users/Kosta/.codex-photongaming"


# API-key resolution for fallback entries.

from hermes_cli.fallback_config import resolve_entry_api_key


class TestResolveEntryApiKey:
    def test_inline_api_key_wins(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"provider": "custom", "api_key": "inline-key", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "inline-key"

    def test_key_env_resolves_from_environment(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "env-key"

    def test_api_key_env_alias(self, monkeypatch):
        monkeypatch.setenv("FB_ALIAS_KEY", "alias-key")
        assert resolve_entry_api_key({"api_key_env": "FB_ALIAS_KEY"}) == "alias-key"

    def test_unset_env_var_returns_none(self, monkeypatch):
        monkeypatch.delenv("FB_MISSING", raising=False)
        # None (not "") lets resolve_runtime_provider fall through to the
        # provider's standard credential resolution.
        assert resolve_entry_api_key({"key_env": "FB_MISSING"}) is None

    def test_empty_env_var_returns_none(self, monkeypatch):
        monkeypatch.setenv("FB_EMPTY", "   ")
        assert resolve_entry_api_key({"key_env": "FB_EMPTY"}) is None

    def test_no_key_fields_returns_none(self):
        assert resolve_entry_api_key({"provider": "openrouter", "model": "glm"}) is None

    def test_non_dict_returns_none(self):
        assert resolve_entry_api_key(None) is None
        assert resolve_entry_api_key("nope") is None  # type: ignore[arg-type]

    def test_whitespace_inline_key_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"api_key": "   ", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "env-key"
