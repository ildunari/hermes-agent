"""Tests for the model-providers plugin discovery system.

Verifies that:
 1. All bundled providers at plugins/model-providers/<name>/ are discovered
 2. User plugins at $HERMES_HOME/plugins/model-providers/<name>/ override bundled
 3. plugin.yaml manifests with kind=model-provider are correctly categorized
"""

from __future__ import annotations

import sys
from pathlib import Path



REPO_ROOT = Path(__file__).resolve().parents[2]


def _clear_provider_caches():
    """Force providers/__init__.py to re-discover on next list_providers()."""
    import providers as _pkg
    _pkg._reset_for_tests()
    # Evict any cached plugin modules so the next import re-executes.
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("plugins.model_providers")
            or mod.startswith("_hermes_user_provider")
        ):
            del sys.modules[mod]


def test_bundled_plugins_discovered():
    """Every plugins/model-providers/<name>/ should contain a plugin.yaml + __init__.py."""
    plugins_dir = REPO_ROOT / "plugins" / "model-providers"
    assert plugins_dir.is_dir(), f"Missing {plugins_dir}"

    child_dirs = [c for c in plugins_dir.iterdir() if c.is_dir()]
    assert len(child_dirs) >= 28, f"Expected at least 28 provider plugins, found {len(child_dirs)}"

    for child in child_dirs:
        assert (child / "__init__.py").exists(), f"{child.name} missing __init__.py"
        assert (child / "plugin.yaml").exists(), f"{child.name} missing plugin.yaml"


def test_all_profiles_register():
    """After discovery, the registry must contain every bundled provider directory.

    This is an invariant — the number of profiles matches the number of plugin
    directories, not a hardcoded count. Counts shift when providers are
    added/removed; that's expected and shouldn't break CI.
    """
    _clear_provider_caches()
    from providers import list_providers

    plugins_dir = REPO_ROOT / "plugins" / "model-providers"
    plugin_dir_count = sum(1 for c in plugins_dir.iterdir() if c.is_dir())

    profiles = list_providers()
    names = sorted(p.name for p in profiles)
    # Some plugin __init__.py files register multiple profiles, so the registry
    # count is >= the directory count (never less).
    assert len(names) >= plugin_dir_count, (
        f"Expected at least {plugin_dir_count} profiles (one per plugin dir), got {len(names)}: {names}"
    )

    # Spot-check representative providers from different categories
    for required in (
        "openrouter", "anthropic", "custom", "bedrock", "openai-codex",
        "minimax-oauth", "gmi", "xiaomi", "alibaba-coding-plan", "fireworks",
        "nebius-token-factory",
    ):
        assert required in names, f"Missing profile: {required}"


def test_user_plugin_overrides_bundled(tmp_path, monkeypatch):
    """A user plugin with the same name must override the bundled profile."""
    # Point HERMES_HOME at a fresh temp dir
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # get_hermes_home() may be module-cached depending on codebase; ensure the
    # env var is the source of truth. Most code paths re-read it each call.

    # Drop a user plugin that replaces 'gmi'
    user_gmi = hermes_home / "plugins" / "model-providers" / "gmi"
    user_gmi.mkdir(parents=True)
    (user_gmi / "__init__.py").write_text(
        "from providers import register_provider\n"
        "from providers.base import ProviderProfile\n"
        "\n"
        "custom_gmi = ProviderProfile(\n"
        '    name="gmi",\n'
        '    aliases=("gmi-user-override-test",),\n'
        '    env_vars=("GMI_API_KEY",),\n'
        '    base_url="https://user-override.example.com/v1",\n'
        '    auth_type="api_key",\n'
        ")\n"
        "register_provider(custom_gmi)\n"
    )
    (user_gmi / "plugin.yaml").write_text(
        "name: gmi-user-override\n"
        "kind: model-provider\n"
        "version: 0.0.1\n"
        "description: Test user override\n"
    )

    _clear_provider_caches()
    from providers import get_provider_profile

    gmi = get_provider_profile("gmi")
    assert gmi is not None
    assert gmi.base_url == "https://user-override.example.com/v1", (
        f"User override not applied; got base_url={gmi.base_url!r}"
    )
    assert "gmi-user-override-test" in gmi.aliases

    # Clean up: reset discovery state so other tests see the bundled version
    _clear_provider_caches()


def test_disabled_user_provider_unloads_and_reloads_per_profile(tmp_path, monkeypatch):
    """A profile deny-list removes policy without disturbing bundled providers."""
    import providers

    hermes_home = tmp_path / ".hermes"
    plugin_dir = hermes_home / "plugins" / "model-providers" / "policy-proxy"
    plugin_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (plugin_dir / "__init__.py").write_text(
        "from providers import register_provider\n"
        "from providers.base import ProviderProfile\n"
        "profile = ProviderProfile(\n"
        "    name='policy-proxy',\n"
        "    fallback_models=('claude-opus-test',),\n"
        "    model_aliases={'opus': 'claude-opus'},\n"
        "    model_alias_priority=10,\n"
        ")\n"
        "register_provider(profile)\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.yaml").write_text(
        "name: policy-proxy-provider\nkind: model-provider\nversion: 1\n",
        encoding="utf-8",
    )
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "plugins:\n  disabled:\n    - policy-proxy-provider\n",
        encoding="utf-8",
    )

    _clear_provider_caches()
    assert providers.get_provider_profile("policy-proxy") is None

    config_path.write_text("plugins:\n  disabled: []\n", encoding="utf-8")
    providers.unload_provider_plugins(scope=hermes_home)
    loaded = providers.get_provider_profile("policy-proxy")
    assert loaded is not None
    assert loaded.fallback_models == ("claude-opus-test",)

    config_path.write_text(
        "plugins:\n  disabled:\n    - policy-proxy-provider\n",
        encoding="utf-8",
    )
    providers.unload_provider_plugins(scope=hermes_home)
    assert providers.get_provider_profile("policy-proxy") is None

    _clear_provider_caches()


def test_failed_user_plugin_registration_is_rolled_back(tmp_path, monkeypatch):
    import providers

    home = tmp_path / "profile"
    plugin_dir = home / "plugins" / "model-providers" / "broken"
    plugin_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (plugin_dir / "__init__.py").write_text(
        "from providers import register_provider\n"
        "from providers.base import ProviderProfile\n"
        "register_provider(ProviderProfile(name='partial-hijack', aliases=('anthropic',)))\n"
        "raise RuntimeError('load failed after registration')\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.yaml").write_text(
        "name: broken-provider\nkind: model-provider\nversion: 1\n",
        encoding="utf-8",
    )

    _clear_provider_caches()
    try:
        assert providers.get_provider_profile("partial-hijack", scope=home) is None
        native = providers.get_provider_profile("anthropic", scope=home)
        assert native is not None and native.name == "anthropic"
    finally:
        _clear_provider_caches()


def test_user_plugin_module_names_do_not_collide_after_normalization(
    tmp_path, monkeypatch
):
    import providers

    home = tmp_path / "profile"
    root = home / "plugins" / "model-providers"
    monkeypatch.setenv("HERMES_HOME", str(home))
    for directory, provider_name in (("a-b", "dash-provider"), ("a_b", "underscore-provider")):
        plugin_dir = root / directory
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text(
            "from providers import register_provider\n"
            "from providers.base import ProviderProfile\n"
            f"register_provider(ProviderProfile(name='{provider_name}'))\n",
            encoding="utf-8",
        )

    _clear_provider_caches()
    try:
        assert providers.get_provider_profile("dash-provider", scope=home) is not None
        assert providers.get_provider_profile("underscore-provider", scope=home) is not None
    finally:
        _clear_provider_caches()


def test_unload_evicts_relative_submodules_before_reload(tmp_path, monkeypatch):
    """Reload must not retain a stale catalog module from the prior generation."""
    import importlib

    import providers

    home = tmp_path / "profile"
    plugin_dir = home / "plugins" / "model-providers" / "reloadable"
    plugin_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (plugin_dir / "__init__.py").write_text(
        "from providers import register_provider\n"
        "from providers.base import ProviderProfile\n"
        "from .catalog import MODELS\n"
        "register_provider(ProviderProfile(name='reloadable', fallback_models=MODELS))\n",
        encoding="utf-8",
    )
    catalog = plugin_dir / "catalog.py"
    catalog.write_text("MODELS = ('first-model',)\n", encoding="utf-8")

    _clear_provider_caches()
    try:
        first = providers.get_provider_profile("reloadable", scope=home)
        assert first is not None and first.fallback_models == ("first-model",)

        catalog.write_text(
            "MODELS = ('second-model-with-new-size',)\n", encoding="utf-8"
        )
        importlib.invalidate_caches()
        providers.unload_provider_plugins(scope=home)
        second = providers.get_provider_profile("reloadable", scope=home)
        assert second is not None
        assert second.fallback_models == ("second-model-with-new-size",)
    finally:
        _clear_provider_caches()
