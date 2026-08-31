"""Provider profile registry and lazy plugin discovery.

Provider profiles can be bundled, installed for one ``HERMES_HOME`` profile,
or exposed through an enabled Python entry point. Bundled and legacy profiles
form a process-global base. User and entry-point registrations are isolated by
Hermes home so a multiplexed process never carries one profile's provider
catalog or routing policy into another profile.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import sys
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from hermes_constants import (
    hermes_home_key,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from providers.base import OMIT_TEMPERATURE, ProviderProfile  # noqa: F401

logger = logging.getLogger(__name__)

# Process-global bundled/legacy registrations. These names remain public for
# backward compatibility with tests and third-party code that snapshots the
# historical registry directly.
_REGISTRY: dict[str, ProviderProfile] = {}
_ALIASES: dict[str, str] = {}
_PROVIDER_LIST_CACHE: list[ProviderProfile] | None = None
_discovered = False

# User and entry-point registrations are overlays keyed by HERMES_HOME.
_SCOPED_REGISTRIES: dict[str, dict[str, ProviderProfile]] = {}
_SCOPED_ALIASES: dict[str, dict[str, str]] = {}
_SCOPED_PROVIDER_LIST_CACHE: dict[str, list[ProviderProfile]] = {}
_DISCOVERED_SCOPES: set[str] = set()
_SCOPED_MODULES: dict[str, set[str]] = {}
# Bare-module entry points self-register only on their first Python import.
# Retain the profiles that import produced so later Hermes-home scopes can
# receive the same opt-in provider without re-executing an already-cached module.
_ENTRY_POINT_PROVIDER_TEMPLATES: dict[str, tuple[ProviderProfile, ...]] = {}

# Module-level plugin code calls register_provider(profile) with no scope. The
# discovery context supplies the correct target without changing that stable
# authoring contract. ContextVars keep concurrent multiplex-profile discovery
# isolated by task/thread.
_REGISTRATION_SCOPE: ContextVar[str | None] = ContextVar(
    "provider_registration_scope", default=None
)
_REGISTRATION_PRECEDENCE: ContextVar[str] = ContextVar(
    "provider_registration_precedence", default="user"
)
_DISCOVERY_LOCK = threading.RLock()

# Repo-root ``plugins/model-providers/`` — populated at discovery time.
_BUNDLED_PLUGINS_DIR = (
    Path(__file__).resolve().parent.parent / "plugins" / "model-providers"
)


def _normalize_name(value: str) -> str:
    return str(value or "").strip().lower()


def _rebuild_aliases(registry: dict[str, ProviderProfile]) -> dict[str, str]:
    owners: dict[str, set[str]] = {}
    for canonical in sorted(registry):
        profile = registry[canonical]
        for alias in profile.aliases:
            key = _normalize_name(alias)
            if key:
                owners.setdefault(key, set()).add(canonical)
    # Collisions are intentionally absent: callers must use a canonical name
    # rather than having filesystem/load order choose an endpoint.
    return {
        alias: next(iter(canonicals))
        for alias, canonicals in owners.items()
        if len(canonicals) == 1
    }


def _validate_profile_metadata(profile: ProviderProfile) -> None:
    """Reject malformed routing metadata before it can affect other profiles.

    Provider modules are an extension boundary.  In particular, one plugin's
    invalid priority must not make the alias resolver abandon every otherwise
    valid candidate and fall through to a metered native provider.
    """
    if profile.model_alias_priority is not None and (
        isinstance(profile.model_alias_priority, bool)
        or not isinstance(profile.model_alias_priority, int)
    ):
        raise TypeError("Provider profile .model_alias_priority must be an int or None")

    aliases: Any = profile.model_aliases
    if isinstance(aliases, Mapping):
        invalid = any(
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(family, str)
            or not family.strip()
            for alias, family in aliases.items()
        )
    elif isinstance(aliases, (tuple, list, set, frozenset)):
        invalid = any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
    else:
        invalid = True
    if invalid:
        raise TypeError(
            "Provider profile .model_aliases must contain non-empty string aliases"
        )

    if not isinstance(profile.fallback_models, (tuple, list)) or any(
        not isinstance(model, str) or not model.strip()
        for model in profile.fallback_models
    ):
        raise TypeError(
            "Provider profile .fallback_models must contain non-empty model strings"
        )


def _invalidate_scope(scope_key: str) -> None:
    _SCOPED_PROVIDER_LIST_CACHE.pop(scope_key, None)


def _restore_registry_snapshot(
    scope_key: str | None,
    snapshot: dict[str, ProviderProfile],
) -> None:
    """Roll back registrations made by one plugin that failed to load."""
    global _PROVIDER_LIST_CACHE
    if scope_key is None:
        _REGISTRY.clear()
        _REGISTRY.update(snapshot)
        _ALIASES.clear()
        _ALIASES.update(_rebuild_aliases(_REGISTRY))
        _PROVIDER_LIST_CACHE = None
        _SCOPED_PROVIDER_LIST_CACHE.clear()
        return

    if snapshot:
        _SCOPED_REGISTRIES[scope_key] = dict(snapshot)
        _SCOPED_ALIASES[scope_key] = _rebuild_aliases(snapshot)
    else:
        _SCOPED_REGISTRIES.pop(scope_key, None)
        _SCOPED_ALIASES.pop(scope_key, None)
    _invalidate_scope(scope_key)


def register_provider(
    profile: ProviderProfile,
    *,
    scope: str | Path | None = None,
) -> None:
    """Register a provider profile by name and aliases.

    Calls made by bundled modules register in the process-global base. Calls
    made while a user plugin or enabled entry point is being discovered register
    in that profile's scoped overlay. ``scope=`` is public primarily for plugin
    lifecycle managers and tests that already know their immutable home.

    User registrations replace bundled profiles of the same canonical name.
    Entry-point registrations are lower precedence than bundled profiles and are
    therefore ignored on a canonical-name collision. Registration order never
    participates in model alias preference; that policy lives on
    :class:`ProviderProfile` metadata.
    """
    if not isinstance(profile, ProviderProfile):
        raise TypeError(
            "register_provider() expects a ProviderProfile instance, "
            f"got {type(profile).__name__}"
        )
    canonical = _normalize_name(profile.name)
    if not canonical:
        raise ValueError("Provider profile .name must be a non-empty string")
    if canonical != profile.name:
        raise ValueError("Provider profile .name must be normalized lowercase text")
    _validate_profile_metadata(profile)

    explicit_scope = hermes_home_key(scope) if scope is not None else None
    target_scope = explicit_scope or _REGISTRATION_SCOPE.get()
    precedence = _REGISTRATION_PRECEDENCE.get()

    global _PROVIDER_LIST_CACHE
    with _DISCOVERY_LOCK:
        if target_scope is None:
            _REGISTRY[canonical] = profile
            _ALIASES.clear()
            _ALIASES.update(_rebuild_aliases(_REGISTRY))
            _PROVIDER_LIST_CACHE = None
            _SCOPED_PROVIDER_LIST_CACHE.clear()
            return

        # Pip entry points are profile-gated but lower precedence than bundled
        # filesystem profiles. A package cannot shadow a first-party provider.
        if precedence == "entrypoint" and canonical in _REGISTRY:
            return

        registry = _SCOPED_REGISTRIES.setdefault(target_scope, {})
        registry[canonical] = profile
        _SCOPED_ALIASES[target_scope] = _rebuild_aliases(registry)
        _invalidate_scope(target_scope)


def get_provider_profile(
    name: str,
    *,
    scope: str | Path | None = None,
) -> ProviderProfile | None:
    """Look up a provider profile by canonical name or alias for one profile."""
    scope_key = hermes_home_key(scope)
    _discover_providers(scope=scope_key)

    lookup = _normalize_name(name)
    # Custom providers keep their runtime suffix elsewhere but share the
    # built-in custom request-profile quirks.
    if lookup.startswith("custom:"):
        lookup = "custom"

    scoped_registry = _SCOPED_REGISTRIES.get(scope_key, {})
    scoped_aliases = _SCOPED_ALIASES.get(scope_key, {})

    # Built-in canonical names and aliases are reserved. A user plugin may
    # deliberately replace a built-in by registering the same canonical name,
    # but cannot redirect ``anthropic``/``claude`` merely by claiming either as
    # an alias or by registering a new profile under an existing built-in alias.
    if lookup in _REGISTRY:
        return scoped_registry.get(lookup) or _REGISTRY[lookup]

    canonical = _ALIASES.get(lookup)
    if canonical:
        return scoped_registry.get(canonical) or _REGISTRY.get(canonical)

    if lookup in scoped_registry:
        return scoped_registry[lookup]

    canonical = scoped_aliases.get(lookup)
    if canonical:
        return scoped_registry.get(canonical)
    return None


def list_providers(
    *,
    scope: str | Path | None = None,
) -> list[ProviderProfile]:
    """Return active profiles in deterministic canonical-name order."""
    global _PROVIDER_LIST_CACHE
    scope_key = hermes_home_key(scope)
    _discover_providers(scope=scope_key)

    cached = _SCOPED_PROVIDER_LIST_CACHE.get(scope_key)
    if cached is not None:
        return list(cached)

    with _DISCOVERY_LOCK:
        merged = dict(_REGISTRY)
        merged.update(_SCOPED_REGISTRIES.get(scope_key, {}))
        result = [merged[name] for name in sorted(merged)]
        _SCOPED_PROVIDER_LIST_CACHE[scope_key] = result
        # Preserve the old process-global cache as a snapshot of the bundled
        # base only; scope-aware callers use the cache above.
        _PROVIDER_LIST_CACHE = [_REGISTRY[name] for name in sorted(_REGISTRY)]
    return list(result)


def snapshot_registration(
    name: str,
    *,
    scope: str | Path | None = None,
) -> ProviderProfile | None:
    """Return exactly the registration in one layer (without fallback)."""
    canonical = _normalize_name(name)
    if scope is None:
        return _REGISTRY.get(canonical)
    return _SCOPED_REGISTRIES.get(hermes_home_key(scope), {}).get(canonical)


def restore_registration(
    name: str,
    current: ProviderProfile,
    previous: ProviderProfile | None,
    *,
    scope: str | Path | None = None,
) -> bool:
    """Restore a registration only when ``current`` is still installed.

    This compare-and-restore shape makes plugin unload safe when two reloads or
    managers race: an older owner cannot remove a newer replacement. Removing a
    scoped override immediately restores the bundled/native profile beneath it.
    """
    canonical = _normalize_name(name)
    scope_key = hermes_home_key(scope) if scope is not None else None
    global _PROVIDER_LIST_CACHE
    with _DISCOVERY_LOCK:
        target = (
            _REGISTRY
            if scope_key is None
            else _SCOPED_REGISTRIES.setdefault(scope_key, {})
        )
        if target.get(canonical) is not current:
            return False
        if previous is None:
            target.pop(canonical, None)
        else:
            target[canonical] = previous

        aliases = _rebuild_aliases(target)
        if scope_key is None:
            _ALIASES.clear()
            _ALIASES.update(aliases)
            _PROVIDER_LIST_CACHE = None
            _SCOPED_PROVIDER_LIST_CACHE.clear()
        else:
            if target:
                _SCOPED_ALIASES[scope_key] = aliases
            else:
                _SCOPED_REGISTRIES.pop(scope_key, None)
                _SCOPED_ALIASES.pop(scope_key, None)
            _invalidate_scope(scope_key)
        return True


def unload_provider_plugins(*, scope: str | Path | None = None) -> None:
    """Unload every profile-scoped provider registration for one Hermes home.

    The bundled registry is untouched. The next lookup rediscovers the scope,
    so a provider newly listed under ``plugins.disabled`` stays unloaded and a
    removed override cleanly reveals the native provider underneath it.
    """
    scope_key = hermes_home_key(scope)
    with _DISCOVERY_LOCK:
        _SCOPED_REGISTRIES.pop(scope_key, None)
        _SCOPED_ALIASES.pop(scope_key, None)
        _SCOPED_PROVIDER_LIST_CACHE.pop(scope_key, None)
        _DISCOVERED_SCOPES.discard(scope_key)
        for module_name in _SCOPED_MODULES.pop(scope_key, set()):
            sys.modules.pop(module_name, None)


def _reset_for_tests() -> None:
    """Clear every registry/discovery layer. Test-only."""
    global _PROVIDER_LIST_CACHE, _discovered
    with _DISCOVERY_LOCK:
        _REGISTRY.clear()
        _ALIASES.clear()
        _PROVIDER_LIST_CACHE = None
        _discovered = False
        _SCOPED_REGISTRIES.clear()
        _SCOPED_ALIASES.clear()
        _SCOPED_PROVIDER_LIST_CACHE.clear()
        _DISCOVERED_SCOPES.clear()
        _ENTRY_POINT_PROVIDER_TEMPLATES.clear()
        for modules in _SCOPED_MODULES.values():
            for module_name in modules:
                sys.modules.pop(module_name, None)
        _SCOPED_MODULES.clear()


def _user_plugins_dir(scope_key: str) -> Path | None:
    """Return ``<scope>/plugins/model-providers`` when it exists."""
    directory = Path(scope_key) / "plugins" / "model-providers"
    return directory if directory.is_dir() else None


def _plugin_manifest_names(plugin_dir: Path) -> set[str]:
    names = {_normalize_name(plugin_dir.name)}
    manifest = plugin_dir / "plugin.yaml"
    if not manifest.is_file():
        return names
    try:
        import yaml

        payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        if isinstance(payload, dict) and payload.get("name"):
            names.add(_normalize_name(payload["name"]))
    except Exception:
        # A malformed manifest should not prevent the provider loader from
        # reporting the actual module import error independently.
        pass
    return names


def _disabled_user_plugins(scope_key: str) -> set[str]:
    token = set_hermes_home_override(scope_key)
    try:
        from hermes_cli.plugins import _get_disabled_plugins

        return {_normalize_name(name) for name in _get_disabled_plugins()}
    except Exception:
        return set()
    finally:
        reset_hermes_home_override(token)


def _import_plugin_dir(
    plugin_dir: Path,
    source: str,
    *,
    scope_key: str | None = None,
) -> None:
    """Import one provider directory so its module-level registration runs."""
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        return

    safe_name = plugin_dir.name.replace("-", "_")
    if source == "bundled":
        module_name = f"plugins.model_providers.{safe_name}"
    else:
        digest = hashlib.sha256(str(scope_key).encode("utf-8")).hexdigest()[:12]
        plugin_digest = hashlib.sha256(plugin_dir.name.encode("utf-8")).hexdigest()[:8]
        module_name = f"_hermes_user_provider_{digest}_{plugin_digest}_{safe_name}"

    if module_name in sys.modules:
        return

    scope_token = _REGISTRATION_SCOPE.set(scope_key if source == "user" else None)
    precedence_token = _REGISTRATION_PRECEDENCE.set(source)
    target_scope = scope_key if source == "user" else None
    target_registry = (
        _REGISTRY
        if target_scope is None
        else _SCOPED_REGISTRIES.get(target_scope, {})
    )
    registry_snapshot = dict(target_registry)
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, init_file, submodule_search_locations=[str(plugin_dir)]
        )
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if scope_key is not None:
            owned_modules = {
                name
                for name in sys.modules
                if name == module_name or name.startswith(f"{module_name}.")
            }
            _SCOPED_MODULES.setdefault(scope_key, set()).update(owned_modules)
    except Exception as exc:
        _restore_registry_snapshot(target_scope, registry_snapshot)
        logger.warning(
            "Failed to load %s provider plugin %s: %s", source, plugin_dir.name, exc
        )
        for name in tuple(sys.modules):
            if name == module_name or name.startswith(f"{module_name}."):
                sys.modules.pop(name, None)
    finally:
        _REGISTRATION_PRECEDENCE.reset(precedence_token)
        _REGISTRATION_SCOPE.reset(scope_token)


def _discover_entry_point_providers(scope_key: str) -> None:
    """Load enabled zero-argument provider entry points into one profile."""
    try:
        import importlib.metadata as metadata
    except Exception:  # pragma: no cover
        return

    token = set_hermes_home_override(scope_key)
    try:
        from hermes_cli.plugins import _get_disabled_plugins, _get_enabled_plugins

        enabled = _get_enabled_plugins()
        disabled = _get_disabled_plugins()
    except Exception:
        enabled, disabled = None, set()
    finally:
        reset_hermes_home_override(token)
    if not enabled:
        return

    try:
        entry_points = metadata.entry_points()
        if hasattr(entry_points, "select"):
            group_entries = list(entry_points.select(group="hermes_agent.plugins"))
        else:  # pragma: no cover
            group_entries = list(entry_points.get("hermes_agent.plugins", []))
    except Exception as exc:
        logger.debug("entry-point provider scan skipped: %s", exc)
        return

    for entry_point in sorted(group_entries, key=lambda item: item.name):
        if entry_point.name not in enabled or entry_point.name in disabled:
            continue
        scope_token = _REGISTRATION_SCOPE.set(scope_key)
        precedence_token = _REGISTRATION_PRECEDENCE.set("entrypoint")
        registry_snapshot = dict(_SCOPED_REGISTRIES.get(scope_key, {}))
        template_key = f"{entry_point.name}:{getattr(entry_point, 'value', '')}"
        try:
            loaded = entry_point.load()
            if callable(loaded):
                if _requires_arguments(loaded):
                    _restore_registry_snapshot(scope_key, registry_snapshot)
                    continue
                loaded()
            else:
                current = _SCOPED_REGISTRIES.get(scope_key, {})
                registered = tuple(
                    current[name]
                    for name in sorted(current)
                    if registry_snapshot.get(name) is not current[name]
                )
                if registered:
                    _ENTRY_POINT_PROVIDER_TEMPLATES[template_key] = registered
                else:
                    # ``entry_point.load()`` returned an already-imported bare
                    # module, so its registration side effect did not run for
                    # this scope. Replay only the validated profiles captured
                    # from that same entry point's first import.
                    for profile in _ENTRY_POINT_PROVIDER_TEMPLATES.get(
                        template_key, ()
                    ):
                        register_provider(profile, scope=scope_key)
        except Exception as exc:
            _restore_registry_snapshot(scope_key, registry_snapshot)
            logger.warning(
                "Failed to load entry-point provider plugin %r: %s",
                entry_point.name,
                exc,
            )
        finally:
            _REGISTRATION_PRECEDENCE.reset(precedence_token)
            _REGISTRATION_SCOPE.reset(scope_token)


def _requires_arguments(fn) -> bool:
    import inspect

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover
        return False
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ) and parameter.default is inspect.Parameter.empty:
            return True
    return False


def _discover_global_providers() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True

    if _BUNDLED_PLUGINS_DIR.is_dir():
        for child in sorted(_BUNDLED_PLUGINS_DIR.iterdir()):
            if child.is_dir() and not child.name.startswith(("_", ".")):
                _import_plugin_dir(child, "bundled")

    # Legacy single-file profiles remain a process-global compatibility layer.
    try:
        import pkgutil

        import providers as package

        for _importer, module_name, _is_package in pkgutil.iter_modules(package.__path__):
            if module_name.startswith("_") or module_name == "base":
                continue
            try:
                importlib.import_module(f"providers.{module_name}")
            except ImportError as exc:
                logger.warning(
                    "Failed to import legacy provider module %s: %s", module_name, exc
                )
    except Exception:
        pass


def _discover_scope(scope_key: str) -> None:
    if scope_key in _DISCOVERED_SCOPES:
        return
    _DISCOVERED_SCOPES.add(scope_key)

    # Entry points are profile-gated and lower precedence than bundled profiles.
    _discover_entry_point_providers(scope_key)

    user_dir = _user_plugins_dir(scope_key)
    if user_dir is None:
        return
    disabled = _disabled_user_plugins(scope_key)
    for child in sorted(user_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        if _plugin_manifest_names(child) & disabled:
            logger.debug("Disabled provider plugin skipped: %s", child.name)
            continue
        _import_plugin_dir(child, "user", scope_key=scope_key)


def _discover_providers(*, scope: str | Path | None = None) -> None:
    """Populate the global provider base and one profile-scoped overlay."""
    scope_key = hermes_home_key(scope)
    with _DISCOVERY_LOCK:
        _discover_global_providers()
        _discover_scope(scope_key)
