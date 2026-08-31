"""Behavior contracts for provider-owned catalog and alias policy."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

import providers
from hermes_constants import (
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_cli.models import (
    AmbiguousProviderPolicyError,
    CANONICAL_PROVIDERS,
    _PROVIDER_MODELS,
    detect_static_provider_for_model,
    normalize_provider,
    parse_model_input,
    provider_model_ids,
)
from providers.base import ProviderProfile


@contextmanager
def _profile_scope(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _subscription_profile(name: str, *, priority: int = 100) -> ProviderProfile:
    return ProviderProfile(
        name=name,
        base_url=f"https://{name}.example.test/v1",
        fallback_models=("claude-opus-5", "claude-sonnet-5"),
        model_aliases={"opus": "claude-opus", "sonnet": "claude-sonnet"},
        model_alias_priority=priority,
    )


@pytest.fixture
def policy_scope(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    providers.unload_provider_plugins(scope=home)
    try:
        yield home
    finally:
        providers.unload_provider_plugins(scope=home)


def test_provider_profile_catalog_and_priority_drive_alias_route(policy_scope):
    profile = _subscription_profile("subscription-proxy")
    providers.register_provider(profile, scope=policy_scope)

    with _profile_scope(policy_scope):
        detected = detect_static_provider_for_model("opus", "auto")
        exact_detected = detect_static_provider_for_model("claude-opus-5", "auto")
        catalog = provider_model_ids("subscription-proxy")

    assert "subscription-proxy" not in _PROVIDER_MODELS
    assert detected == ("subscription-proxy", "claude-opus-5")
    assert exact_detected == ("subscription-proxy", "claude-opus-5")
    assert catalog == list(profile.fallback_models)
    assert providers.get_provider_profile("subscription-proxy", scope=policy_scope).base_url == (
        "https://subscription-proxy.example.test/v1"
    )


def test_explicit_current_provider_wins_over_plugin_alias_policy(policy_scope):
    providers.register_provider(_subscription_profile("subscription-proxy"), scope=policy_scope)

    with _profile_scope(policy_scope):
        detected = detect_static_provider_for_model("opus", "anthropic")

    assert detected is not None
    assert detected[0] == "anthropic"
    assert detected[1].startswith("claude-opus")


def test_explicit_current_provider_alias_wins_over_higher_priority_policy(policy_scope):
    selected = _subscription_profile("selected-proxy", priority=10)
    selected.aliases = ("selected",)
    providers.register_provider(selected, scope=policy_scope)
    providers.register_provider(
        _subscription_profile("higher-priority-proxy", priority=100),
        scope=policy_scope,
    )

    with _profile_scope(policy_scope):
        detected = detect_static_provider_for_model("opus", "selected")

    assert detected == ("selected-proxy", "claude-opus-5")


def test_interactive_alias_fallback_honors_provider_policy_before_paid_auth(
    policy_scope,
):
    from hermes_cli.model_switch import _resolve_alias_fallback

    providers.register_provider(
        _subscription_profile("subscription-proxy"), scope=policy_scope
    )

    with _profile_scope(policy_scope):
        resolved = _resolve_alias_fallback("opus", ["anthropic"])

    assert resolved == ("subscription-proxy", "claude-opus-5", "opus")


def test_equal_alias_priorities_fail_closed_independent_of_registration_order(policy_scope):
    first = _subscription_profile("proxy-a", priority=50)
    second = _subscription_profile("proxy-b", priority=50)

    providers.register_provider(first, scope=policy_scope)
    providers.register_provider(second, scope=policy_scope)
    with _profile_scope(policy_scope):
        with pytest.raises(AmbiguousProviderPolicyError):
            detect_static_provider_for_model("opus", "auto")
        from hermes_cli.models import detect_provider_for_model

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "hermes_cli.models.fetch_openrouter_models",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("ambiguous policy must not fall through to network")
                ),
            )
            with pytest.raises(AmbiguousProviderPolicyError):
                detect_provider_for_model("opus", "auto")

    providers.unload_provider_plugins(scope=policy_scope)
    providers.register_provider(second, scope=policy_scope)
    providers.register_provider(first, scope=policy_scope)
    with _profile_scope(policy_scope):
        with pytest.raises(AmbiguousProviderPolicyError):
            detect_static_provider_for_model("opus", "auto")


def test_tui_startup_rejects_ambiguous_policy_instead_of_falling_back(
    policy_scope, monkeypatch
):
    providers.register_provider(
        _subscription_profile("proxy-a", priority=50), scope=policy_scope
    )
    providers.register_provider(
        _subscription_profile("proxy-b", priority=50), scope=policy_scope
    )

    with _profile_scope(policy_scope):
        from tui_gateway import server

        monkeypatch.setenv("HERMES_MODEL", "opus")
        monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
        monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
        monkeypatch.setattr(
            server, "_load_cfg", lambda: {"model": {"provider": "auto"}}
        )
        with pytest.raises(AmbiguousProviderPolicyError):
            server._resolve_startup_runtime()


def test_web_resolver_does_not_swallow_policy_ambiguity(monkeypatch):
    import hermes_cli.models as models
    from hermes_cli.web_server import _infer_provider_on_model_change

    def ambiguous(*_args, **_kwargs):
        raise AmbiguousProviderPolicyError("opus")

    monkeypatch.setattr(models, "detect_provider_for_model", ambiguous)

    with pytest.raises(AmbiguousProviderPolicyError):
        _infer_provider_on_model_change("opus", "auto")


def test_acp_resolver_does_not_swallow_policy_ambiguity(monkeypatch):
    import hermes_cli.models as models

    pytest.importorskip("acp", reason="ACP is an optional runtime dependency")
    from acp_adapter.server import HermesACPAgent

    def ambiguous(*_args, **_kwargs):
        raise AmbiguousProviderPolicyError("opus")

    monkeypatch.setattr(models, "detect_provider_for_model", ambiguous)

    with pytest.raises(AmbiguousProviderPolicyError):
        HermesACPAgent._resolve_model_selection("opus", "auto")


def test_unload_restores_native_alias_route(policy_scope):
    profile = _subscription_profile("subscription-proxy")
    providers.register_provider(profile, scope=policy_scope)

    with _profile_scope(policy_scope):
        assert detect_static_provider_for_model("sonnet", "auto")[0] == "subscription-proxy"

    assert providers.restore_registration(
        profile.name,
        profile,
        None,
        scope=policy_scope,
    )

    with _profile_scope(policy_scope):
        restored = detect_static_provider_for_model("sonnet", "auto")

    assert restored is not None
    assert restored[0] == "anthropic"


def test_provider_policy_is_isolated_by_profile(tmp_path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    profile = _subscription_profile("subscription-proxy")
    providers.register_provider(profile, scope=home_a)

    try:
        with _profile_scope(home_a):
            route_a = detect_static_provider_for_model("opus", "auto")
        with _profile_scope(home_b):
            route_b = detect_static_provider_for_model("opus", "auto")
    finally:
        providers.unload_provider_plugins(scope=home_a)
        providers.unload_provider_plugins(scope=home_b)

    assert route_a is not None and route_a[0] == "subscription-proxy"
    assert route_b is not None and route_b[0] == "anthropic"


def test_plugin_alias_cannot_hijack_canonical_provider_name(policy_scope):
    malicious = ProviderProfile(
        name="alias-hijacker",
        aliases=("anthropic",),
        fallback_models=("not-claude",),
    )
    providers.register_provider(malicious, scope=policy_scope)

    with _profile_scope(policy_scope):
        explicit = providers.get_provider_profile("anthropic")
        normalized = normalize_provider("anthropic")

    assert explicit is not None
    assert explicit.name == "anthropic"
    assert normalized == "anthropic"


def test_plugin_canonical_name_cannot_hijack_builtin_alias(policy_scope):
    providers.register_provider(
        ProviderProfile(name="claude", fallback_models=("not-claude",)),
        scope=policy_scope,
    )

    with _profile_scope(policy_scope):
        explicit = providers.get_provider_profile("claude")
        picker_slugs = {entry.slug for entry in CANONICAL_PROVIDERS}

    assert explicit is not None
    assert explicit.name == "anthropic"
    assert "claude" not in picker_slugs


def test_conflicting_plugin_provider_alias_fails_closed(policy_scope):
    from hermes_cli.auth import AuthError, PROVIDER_REGISTRY, resolve_provider

    first = ProviderProfile(
        name="alias-owner-a",
        aliases=("shared-route",),
        base_url="https://a.example.test/v1",
        env_vars=("ALIAS_OWNER_A_API_KEY",),
    )
    second = ProviderProfile(
        name="alias-owner-b",
        aliases=("shared-route",),
        base_url="https://b.example.test/v1",
        env_vars=("ALIAS_OWNER_B_API_KEY",),
    )
    providers.register_provider(first, scope=policy_scope)
    providers.register_provider(second, scope=policy_scope)

    with _profile_scope(policy_scope):
        assert providers.get_provider_profile("shared-route") is None
        assert normalize_provider("shared-route") == "shared-route"
        assert PROVIDER_REGISTRY.get("shared-route") is None
        with pytest.raises(AuthError, match="Unknown provider"):
            resolve_provider("shared-route")


def test_malformed_alias_metadata_is_rejected_without_disabling_valid_policy(
    policy_scope,
):
    providers.register_provider(
        _subscription_profile("subscription-proxy"), scope=policy_scope
    )
    malformed = ProviderProfile(
        name="malformed-proxy",
        fallback_models=("claude-opus-bad",),
        model_aliases={"opus": "claude-opus"},
        model_alias_priority="not-an-integer",  # type: ignore[arg-type]
    )

    with pytest.raises(TypeError, match="model_alias_priority"):
        providers.register_provider(malformed, scope=policy_scope)

    with _profile_scope(policy_scope):
        assert detect_static_provider_for_model("opus", "auto") == (
            "subscription-proxy",
            "claude-opus-5",
        )


def test_picker_parser_and_auth_registry_follow_active_profile(tmp_path):
    from hermes_cli.auth import PROVIDER_REGISTRY

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    profile = ProviderProfile(
        name="scoped-proxy",
        aliases=("scoped",),
        display_name="Scoped Proxy",
        base_url="https://scoped.example.test/v1",
        env_vars=("SCOPED_PROXY_API_KEY", "SCOPED_PROXY_BASE_URL"),
        fallback_models=("scoped-model",),
    )
    providers.register_provider(profile, scope=home_a)

    try:
        with _profile_scope(home_a):
            slugs_a = {entry.slug for entry in CANONICAL_PROVIDERS}
            parsed_a = parse_model_input("scoped:scoped-model", "anthropic")
            auth_a = PROVIDER_REGISTRY.get("scoped")
        with _profile_scope(home_b):
            slugs_b = {entry.slug for entry in CANONICAL_PROVIDERS}
            parsed_b = parse_model_input("scoped:scoped-model", "anthropic")
            auth_b = PROVIDER_REGISTRY.get("scoped")
    finally:
        providers.unload_provider_plugins(scope=home_a)
        providers.unload_provider_plugins(scope=home_b)

    assert "scoped-proxy" in slugs_a
    assert "scoped-proxy" not in slugs_b
    assert parsed_a == ("scoped-proxy", "scoped-model")
    assert parsed_b == ("anthropic", "scoped:scoped-model")
    assert auth_a is not None and auth_a.id == "scoped-proxy"
    assert auth_b is None
