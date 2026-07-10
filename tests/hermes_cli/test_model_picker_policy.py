from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.model_picker_policy import (
    load_shared_model_picker_policy,
    merge_model_picker_policy,
    nested_label_map,
    shared_model_picker_path,
)
from hermes_cli.model_switch import (
    apply_model_picker_labels,
    filter_hidden_model_rows,
    load_hidden_model_policy,
)


def test_load_shared_policy_accepts_wrapped_shape(tmp_path):
    path = tmp_path / "model-picker.yaml"
    path.write_text(
        """model_picker:
  hidden_providers: [anthropic, gemini]
  hidden_models:
    openai-codex: [gpt-5.5]
  provider_labels:
    vibeproxy: CLI Proxy
""",
        encoding="utf-8",
    )

    assert load_shared_model_picker_policy(path) == {
        "hidden_providers": ["anthropic", "gemini"],
        "hidden_models": {"openai-codex": ["gpt-5.5"]},
        "provider_labels": {"vibeproxy": "CLI Proxy"},
    }


def test_profile_policy_can_override_one_shared_provider_without_copying_everything():
    shared = {
        "hidden_providers": ["anthropic"],
        "hidden_models": {
            "openai-codex": ["gpt-5.5"],
            "xai-oauth": ["grok-4.3"],
        },
    }
    profile = {
        "model_picker": {
            "hidden_models": {"xai-oauth": ["grok-build-0.1"]},
        }
    }

    merged = merge_model_picker_policy(profile, shared)

    assert merged["hidden_providers"] == ["anthropic"]
    assert merged["hidden_models"] == {
        "openai-codex": ["gpt-5.5"],
        "xai-oauth": ["grok-build-0.1"],
    }


def test_authoritative_shared_policy_ignores_stale_profile_allowlists():
    shared = {
        "authoritative": True,
        "hidden_models": {"openai-codex": ["gpt-5.5"]},
    }
    profile = {
        "model_picker": {
            "visible_models": {"openai-codex": ["gpt-5.6-sol"]},
        }
    }

    assert merge_model_picker_policy(profile, shared) == shared


def test_legacy_hidden_provider_sections_keep_union_semantics():
    merged = merge_model_picker_policy(
        {
            "model_catalog": {"hidden_providers": ["anthropic"]},
            "model_picker": {"hidden_providers": ["gemini", "anthropic"]},
        },
        {},
    )

    assert merged["hidden_providers"] == ["anthropic", "gemini"]


def test_model_policy_normalizes_one_to_one_provider_aliases():
    assert load_hidden_model_policy(
        {}, policy={"hidden_models": {"x-ai": ["grok-old"]}}
    ) == {"xai": ("grok-old",)}


def test_model_label_ids_preserve_case_sensitive_route_spelling():
    assert nested_label_map({"Custom": {"MiniMax-M3": "MiniMax M3"}}) == {
        "custom": {"MiniMax-M3": "MiniMax M3"}
    }


def test_denylist_and_labels_keep_case_sensitive_route_ids_distinct():
    rows = [{"slug": "custom", "models": ["MiniMax-M3", "minimax-m3"]}]

    filtered = filter_hidden_model_rows(rows, {"custom": ("MiniMax-M3",)})
    labeled = apply_model_picker_labels(
        rows,
        {},
        {"custom": {"MiniMax-M3": "Upper", "minimax-m3": "Lower"}},
    )

    assert filtered[0]["models"] == ["minimax-m3"]
    assert labeled[0]["model_labels"] == {
        "MiniMax-M3": "Upper",
        "minimax-m3": "Lower",
    }


def test_shared_policy_path_honors_context_local_named_profile_root(tmp_path):
    root = tmp_path / "custom-hermes"
    profile = root / "profiles" / "coding"
    token = set_hermes_home_override(profile)
    try:
        assert shared_model_picker_path() == root / "shared" / "model-picker.yaml"
    finally:
        reset_hermes_home_override(token)


def test_invalid_shared_policy_warns_and_fails_open(tmp_path, caplog):
    path = tmp_path / "model-picker.yaml"
    path.write_text("model_picker: [not, a, mapping]\n", encoding="utf-8")

    assert load_shared_model_picker_policy(path) == {}
    assert "Ignoring invalid shared model-picker policy" in caplog.text


def test_invalid_rewrite_keeps_last_successful_policy(tmp_path):
    path = tmp_path / "model-picker.yaml"
    path.write_text("model_picker:\n  authoritative: true\n", encoding="utf-8")
    assert load_shared_model_picker_policy(path) == {"authoritative": True}

    path.write_text("model_picker: [broken]\n", encoding="utf-8")
    assert load_shared_model_picker_policy(path) == {"authoritative": True}


def test_semantically_invalid_rewrite_keeps_last_successful_policy(tmp_path, caplog):
    path = tmp_path / "model-picker.yaml"
    path.write_text(
        "model_picker:\n  hidden_models:\n    custom: [old-route]\n",
        encoding="utf-8",
    )
    expected = {"hidden_models": {"custom": ["old-route"]}}
    assert load_shared_model_picker_policy(path) == expected

    path.write_text("model_picker:\n  hidden_models: [broken]\n", encoding="utf-8")
    assert load_shared_model_picker_policy(path) == expected
    assert "hidden_models must be a provider-to-models mapping" in caplog.text
