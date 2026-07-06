import json

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
