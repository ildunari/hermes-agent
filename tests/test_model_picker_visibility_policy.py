from hermes_cli.inventory import ConfigContext, build_models_payload
from hermes_cli.model_switch import (
    expand_hidden_provider_slugs,
    filter_visible_model_rows,
    load_visible_model_policy,
)


def test_load_visible_model_policy_normalizes_provider_aliases():
    cfg = {
        "model_picker": {
            "visible_models": {
                "github-copilot": "gpt-5.4, claude-sonnet-4.6",
                "zai": ["glm-5.2", "glm-5v-turbo"],
            }
        }
    }

    assert load_visible_model_policy(cfg) == {
        "copilot": ("gpt-5.4", "claude-sonnet-4.6"),
        "zai": ("glm-5.2", "glm-5v-turbo"),
    }


def test_x_ai_alias_hides_direct_xai_without_hiding_subscription_oauth():
    assert expand_hidden_provider_slugs(("x-ai",)) == {"xai"}
    assert expand_hidden_provider_slugs(("xai",)) == {"xai", "xai-oauth"}


def test_hidden_policy_can_hide_studio_without_affecting_other_custom_providers():
    assert expand_hidden_provider_slugs(("studio",)) == {"studio"}


def test_filter_visible_model_rows_trims_only_configured_provider():
    rows = [
        {
            "slug": "deepseek",
            "models": ["deepseek-chat", "deepseek-v4-pro", "deepseek-v4-flash"],
            "total_models": 3,
        },
        {"slug": "vibeproxy", "models": ["claude-opus-4-8"], "total_models": 1},
    ]

    filtered = filter_visible_model_rows(
        rows,
        {"deepseek": ("deepseek-v4-pro", "deepseek-v4-flash")},
    )

    assert filtered[0]["models"] == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert filtered[0]["total_models"] == 2
    assert filtered[1] == rows[1]
    assert rows[0]["models"] == ["deepseek-chat", "deepseek-v4-pro", "deepseek-v4-flash"]


def test_explicit_only_keeps_configured_vibeproxy_and_moa_presets(monkeypatch):
    """Desktop chat pickers use explicit_only=True.

    VibeProxy may be emitted by built-in local-proxy discovery rather than the
    user-config section, but a matching ``providers.vibeproxy`` block is still
    explicit configuration. MoA presets are virtual, but configured presets are
    deliberate user model choices and should stay visible too.
    """
    rows = [
        {
            "slug": "moa",
            "name": "Mixture of Agents",
            "models": ["default", "Speed", "Design"],
            "total_models": 3,
            "is_current": False,
            "is_user_defined": False,
        },
        {
            "slug": "vibeproxy",
            "name": "VibeProxy",
            "models": ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5"],
            "total_models": 3,
            "is_current": False,
            "is_user_defined": False,
            "source": "built-in",
        },
        {
            "slug": "ambient",
            "name": "Ambient",
            "models": ["ambient-model"],
            "total_models": 1,
            "is_current": False,
            "is_user_defined": False,
        },
    ]

    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **_kwargs: [dict(row) for row in rows],
    )
    monkeypatch.setattr(
        "hermes_cli.auth.is_provider_explicitly_configured",
        lambda _slug: False,
    )
    monkeypatch.setattr(
        "hermes_cli.inventory._moa_provider_row",
        lambda _current_provider="": dict(rows[0]),
    )

    payload = build_models_payload(
        ConfigContext(
            current_provider="openai-codex",
            current_model="gpt-5.5",
            current_base_url="",
            user_providers={"vibeproxy": {"models": {"claude-opus-4-8": {}}}},
            custom_providers=[],
        ),
        explicit_only=True,
        picker_hints=True,
    )

    by_slug = {row["slug"]: row for row in payload["providers"]}
    assert by_slug["vibeproxy"]["models"] == [
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
    ]
    assert by_slug["moa"]["models"] == ["default", "Speed", "Design"]
    assert "ambient" not in by_slug
