from hermes_cli.inventory import ConfigContext, build_models_payload
from hermes_cli.model_switch import (
    apply_model_picker_labels,
    expand_hidden_provider_slugs,
    filter_hidden_model_rows,
    filter_visible_model_rows,
    list_picker_providers,
    load_model_picker_policy,
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


def test_load_model_picker_policy_reads_profile_config_for_legacy_compatibility(
    monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model_picker": {"hidden_providers": ["legacy"]}},
    )
    monkeypatch.setattr(
        "hermes_cli.model_picker_policy.load_shared_model_picker_policy",
        lambda: {},
    )

    assert load_model_picker_policy() == {"hidden_providers": ["legacy"]}


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
    assert rows[0]["models"] == [
        "deepseek-chat",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]


def test_visible_model_allowlist_keeps_case_sensitive_route_ids_distinct():
    rows = [{"slug": "custom", "models": ["Case-ID", "case-id"]}]

    filtered = filter_visible_model_rows(rows, {"custom": ("Case-ID",)})

    assert filtered[0]["models"] == ["Case-ID"]


def test_hidden_model_policy_allows_new_models_by_default():
    rows = [
        {
            "slug": "openai-codex",
            "models": ["gpt-old", "gpt-kept", "gpt-new"],
            "total_models": 3,
        }
    ]

    filtered = filter_hidden_model_rows(rows, {"openai-codex": ("gpt-old",)})

    assert filtered[0]["models"] == ["gpt-kept", "gpt-new"]
    assert filtered[0]["total_models"] == 2
    assert rows[0]["models"] == ["gpt-old", "gpt-kept", "gpt-new"]


def test_picker_labels_do_not_change_runtime_route_ids():
    rows = [
        {"slug": "vibeproxy", "name": "VibeProxy", "models": ["gemini-3.5-flash-low"]}
    ]

    labeled = apply_model_picker_labels(
        rows,
        {"vibeproxy": "CLI Proxy"},
        {"vibeproxy": {"gemini-3.5-flash-low": "Gemini 3.5 Flash"}},
    )

    assert labeled == [
        {
            "slug": "vibeproxy",
            "name": "CLI Proxy",
            "models": ["gemini-3.5-flash-low"],
            "model_labels": {"gemini-3.5-flash-low": "Gemini 3.5 Flash"},
        }
    ]
    assert rows[0]["name"] == "VibeProxy"


def test_gateway_picker_uses_one_policy_snapshot_for_models_and_labels(monkeypatch):
    calls = 0

    def policy():
        nonlocal calls
        calls += 1
        return {
            "hidden_models": {"vibeproxy": ["old-route"]},
            "provider_labels": {"vibeproxy": "CLI Proxy"},
            "model_labels": {"vibeproxy": {"kept-route": "Kept Model"}},
        }

    monkeypatch.setattr("hermes_cli.model_switch.load_model_picker_policy", policy)
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **_kwargs: [
            {
                "slug": "vibeproxy",
                "name": "VibeProxy",
                "models": ["old-route", "kept-route"],
                "total_models": 2,
            }
        ],
    )

    providers = list_picker_providers(include_moa=False)

    assert calls == 1
    assert providers == [
        {
            "slug": "vibeproxy",
            "name": "CLI Proxy",
            "models": ["kept-route"],
            "total_models": 1,
            "model_labels": {"kept-route": "Kept Model"},
        }
    ]


def test_shared_policy_can_pin_unconfigured_providers_in_explicit_picker(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr("hermes_cli.inventory._moa_provider_row", lambda _current="": None)

    payload = build_models_payload(
        ConfigContext(
            current_provider="",
            current_model="",
            current_base_url="",
            user_providers={},
            custom_providers=[],
            pinned_providers=("deepseek", "zai"),
            hidden_models={
                "deepseek": ("deepseek-chat", "deepseek-reasoner"),
                "zai": ("glm-5.1", "glm-5", "glm-5-turbo", "glm-4.7", "glm-4.5", "glm-4.5-flash"),
            },
        ),
        explicit_only=True,
    )
    by_slug = {row["slug"]: row for row in payload["providers"]}

    assert by_slug["deepseek"]["models"] == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert by_slug["zai"]["models"] == ["glm-5.2", "glm-5v-turbo"]
    assert by_slug["deepseek"]["source"] == "shared-picker-policy"


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
