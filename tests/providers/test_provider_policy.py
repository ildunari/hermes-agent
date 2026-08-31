"""Behavior contracts for provider-owned catalog and alias policy."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

import providers
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.models import (
    _PROVIDER_MODELS,
    detect_static_provider_for_model,
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


def test_equal_alias_priorities_fail_closed_independent_of_registration_order(policy_scope):
    first = _subscription_profile("proxy-a", priority=50)
    second = _subscription_profile("proxy-b", priority=50)

    providers.register_provider(first, scope=policy_scope)
    providers.register_provider(second, scope=policy_scope)
    with _profile_scope(policy_scope):
        forward = detect_static_provider_for_model("opus", "auto")
        from hermes_cli.models import detect_provider_for_model

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "hermes_cli.models.fetch_openrouter_models",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("ambiguous policy must not fall through to network")
                ),
            )
            forward_full = detect_provider_for_model("opus", "auto")

    providers.unload_provider_plugins(scope=policy_scope)
    providers.register_provider(second, scope=policy_scope)
    providers.register_provider(first, scope=policy_scope)
    with _profile_scope(policy_scope):
        reverse = detect_static_provider_for_model("opus", "auto")

    assert forward is None
    assert forward_full is None
    assert reverse is None


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
