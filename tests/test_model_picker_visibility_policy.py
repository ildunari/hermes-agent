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
