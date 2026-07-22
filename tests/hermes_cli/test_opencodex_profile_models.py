"""Behavior contracts for Hermes profiles backed by OpenCodex app-server."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _write_opencodex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    catalog = home / "opencodex-catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6-Sol",
                        "context_window": 372_000,
                        "effective_context_window_percent": 95,
                    },
                    {"slug": "anthropic/claude-opus-4-8", "display_name": "Claude Opus 4.8"},
                    {"slug": "moonshot/kimi-k3", "display_name": "Kimi K3"},
                    {"slug": "xai/grok-4.5", "display_name": "Grok 4.5"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (home / "config.toml").write_text(
        f'model_catalog_json = "{catalog}"\n'
        'openai_base_url = "http://127.0.0.1:10110/v1"\n',
        encoding="utf-8",
    )
    return home


def _profile_config(codex_home: Path) -> dict:
    return {
        "model": {
            "provider": "openai-codex",
            "default": "gpt-5.6-sol",
            "openai_runtime": "codex_app_server",
            "codex_app_server": {
                "codex_home": str(codex_home),
                "forward_model": True,
            },
        }
    }


def test_picker_uses_only_opencodex_catalog_models(tmp_path):
    """The Codex-profile picker is one virtual provider backed by OCX."""
    from hermes_cli.inventory import build_models_payload, load_picker_context

    cfg = _profile_config(_write_opencodex_home(tmp_path))
    with patch("hermes_cli.config.load_config", return_value=cfg):
        payload = build_models_payload(
            load_picker_context(), picker_hints=True, capabilities=True
        )

    assert payload["provider"] == "openai-codex"
    assert [row["slug"] for row in payload["providers"]] == ["openai-codex"]
    assert payload["providers"][0]["models"] == [
        "gpt-5.6-sol",
        "anthropic/claude-opus-4-8",
        "moonshot/kimi-k3",
        "xai/grok-4.5",
    ]
    assert payload["providers"][0]["model_labels"]["xai/grok-4.5"] == "Grok 4.5"


def test_context_length_uses_opencodex_effective_catalog_window(tmp_path):
    """Preflight/UI totals match the effective app-server runtime window."""
    from agent.model_metadata import get_model_context_length

    cfg = _profile_config(_write_opencodex_home(tmp_path))
    with patch("hermes_cli.config.load_config", return_value=cfg):
        context_length = get_model_context_length(
            "gpt-5.6-sol",
            provider="openai-codex",
        )

    assert context_length == 353_400


def test_switching_friendly_grok_name_stays_on_codex_runtime(tmp_path):
    """Hermes provider names must not bypass OpenCodex credential routing."""
    from hermes_cli.model_switch import switch_model

    cfg = _profile_config(_write_opencodex_home(tmp_path))
    with patch("hermes_cli.config.load_config", return_value=cfg):
        result = switch_model(
            "Grok 4.5",
            current_provider="openai-codex",
            current_model="gpt-5.6-sol",
            explicit_provider="xai-oauth",
        )

    assert result.success is True
    assert result.target_provider == "openai-codex"
    assert result.new_model == "xai/grok-4.5"
    assert result.api_mode == "codex_app_server"


def test_opencodex_model_matching_tolerates_safe_name_mutations(tmp_path):
    from hermes_cli.model_switch import switch_model

    cfg = _profile_config(_write_opencodex_home(tmp_path))
    examples = {
        "grok-4.5": "xai/grok-4.5",
        "x-ai:grok_4_5": "xai/grok-4.5",
        "CLAUDE OPUS 4.8": "anthropic/claude-opus-4-8",
        "anthropic:claude_opus_4_8": "anthropic/claude-opus-4-8",
        "kimi-k3": "moonshot/kimi-k3",
    }

    with patch("hermes_cli.config.load_config", return_value=cfg):
        for supplied, canonical in examples.items():
            result = switch_model(
                supplied,
                current_provider="openai-codex",
                current_model="gpt-5.6-sol",
            )
            assert result.success is True, (supplied, result.error_message)
            assert result.target_provider == "openai-codex"
            assert result.new_model == canonical


def test_unknown_model_is_rejected_against_opencodex_catalog(tmp_path):
    from hermes_cli.model_switch import switch_model

    cfg = _profile_config(_write_opencodex_home(tmp_path))
    with patch("hermes_cli.config.load_config", return_value=cfg):
        result = switch_model(
            "grok-9",
            current_provider="openai-codex",
            current_model="gpt-5.6-sol",
        )

    assert result.success is False
    assert "not in the OpenCodex catalog" in result.error_message
    assert "xai/grok-4.5" in result.error_message


def test_provider_hint_disambiguates_future_catalog_name_collision(tmp_path):
    from hermes_cli.opencodex_catalog import load_configured_opencodex_catalog

    home = _write_opencodex_home(tmp_path)
    catalog_path = home / "opencodex-catalog.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    payload["models"].append(
        {"slug": "other/grok-4-5", "display_name": "Grok 4.5"}
    )
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
    catalog = load_configured_opencodex_catalog(_profile_config(home))

    assert catalog is not None
    ambiguous = catalog.resolve("grok 4.5")
    assert ambiguous.model is None
    assert "ambiguous" in ambiguous.error
    hinted = catalog.resolve("grok 4.5", provider_hint="xai-oauth")
    assert hinted.model is not None
    assert hinted.model.slug == "xai/grok-4.5"


def test_configured_but_missing_catalog_fails_closed(tmp_path):
    from hermes_cli.inventory import load_picker_context
    from hermes_cli.model_switch import switch_model
    from hermes_cli.opencodex_catalog import OpenCodexCatalogError

    cfg = _profile_config(tmp_path / "missing-codex-home")
    cfg["model"]["codex_app_server"]["model_catalog_json"] = str(
        tmp_path / "missing-catalog.json"
    )

    with patch("hermes_cli.config.load_config", return_value=cfg):
        with pytest.raises(OpenCodexCatalogError):
            load_picker_context()
        result = switch_model(
            "Grok 4.5",
            current_provider="openai-codex",
            current_model="gpt-5.6-sol",
        )

    assert result.success is False
    assert "OpenCodex catalog is unavailable" in result.error_message
