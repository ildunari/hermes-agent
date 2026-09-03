"""Block alias fallback from subscription/OAuth routes to metered API keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

_OAUTH_AUTH_TYPES = frozenset(
    {
        "oauth_external",
        "oauth_device_code",
        "oauth_minimax",
        "copilot",
        "external_process",
        "aws_sdk",
        "vertex",
    }
)


class MeteredAliasFallbackBlockedError(Exception):
    """Alias fallback refused to hop from subscription/OAuth to metered API key."""

    def __init__(
        self,
        alias: str,
        active_provider: str,
        refused_provider: str,
    ):
        self.alias = alias
        self.active_provider = active_provider
        self.refused_provider = refused_provider
        super().__init__(
            f"alias {alias!r} would fall back from {active_provider!r} to "
            f"metered API-key provider {refused_provider!r}"
        )


def metered_alias_fallback_allowed() -> bool:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        if cfg.get("model_aliases_allow_metered_fallback"):
            return True
        model_cfg = cfg.get("model")
        if isinstance(model_cfg, dict) and model_cfg.get(
            "model_aliases_allow_metered_fallback"
        ):
            return True
    except Exception:
        pass
    return False


def _lookup_provider_profile(provider_id: str):
    """Resolve a profile from the scoped/plugin-aware provider registry."""
    pid = (provider_id or "").strip().lower()
    if not pid:
        return None
    try:
        from providers import get_provider_profile, list_providers

        profile = get_provider_profile(pid)
        if profile is not None:
            return profile
        for candidate in list_providers():
            aliases = tuple(getattr(candidate, "aliases", ()) or ())
            if candidate.name == pid or pid in {str(a).lower() for a in aliases}:
                return candidate
    except Exception:
        pass
    return None


def _overlay_keyless(provider_id: str) -> bool:
    try:
        from providers.base import keyless_api_key_placeholder

        if keyless_api_key_placeholder(provider_id):
            return True
    except Exception:
        pass
    try:
        from hermes_cli.providers import HERMES_OVERLAYS

        overlay = HERMES_OVERLAYS.get(provider_id)
        if overlay is not None and getattr(overlay, "keyless", False):
            return True
    except Exception:
        pass
    return False


def _registry_auth_type(provider_id: str) -> str | None:
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        pconfig = PROVIDER_REGISTRY.get(provider_id)
    except Exception:
        return None
    if pconfig is None:
        return None
    return str(getattr(pconfig, "auth_type", "") or "") or None


def classify_provider_billing(provider_id: str) -> str:
    """Classify a provider as keyless, oauth, metered, or unknown.

    Uses ``ProviderProfile.auth_type`` / ``keyless`` from the scoped plugin
    registry first, then overlays and ``PROVIDER_REGISTRY``. Plugin OAuth and
    keyless profiles are included even when auth.py excludes them from the
    env-auto-select snapshot.
    """
    pid = (provider_id or "").strip().lower()
    if not pid:
        return "unknown"
    profile = _lookup_provider_profile(pid)
    if profile is not None:
        if getattr(profile, "keyless", False):
            return "keyless"
        auth = str(getattr(profile, "auth_type", "") or "").strip()
        if auth in _OAUTH_AUTH_TYPES:
            return "oauth"
        if auth == "api_key":
            return "metered"
    if _overlay_keyless(pid):
        return "keyless"
    auth = _registry_auth_type(pid)
    if not auth:
        return "unknown"
    if auth in _OAUTH_AUTH_TYPES or auth != "api_key":
        return "oauth"
    return "metered"


def provider_is_metered_api_key(provider_id: str) -> bool:
    return classify_provider_billing(provider_id) == "metered"


def active_provider_blocks_metered_fallback(active_provider: str) -> bool:
    return classify_provider_billing(active_provider) in {"keyless", "oauth"}


def aux_metered_fallback_allowed() -> bool:
    """True when auxiliary auto may discover a metered provider (opt-in)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        aux_cfg = cfg.get("auxiliary")
        if isinstance(aux_cfg, dict) and aux_cfg.get("allow_metered_fallback") is True:
            return True
    except Exception:
        pass
    return False


def aux_auto_metered_fallback_blocked(main_provider: str) -> bool:
    """True when aux ``auto`` must not discover a metered provider from *main*."""
    return (
        active_provider_blocks_metered_fallback(main_provider)
        and not aux_metered_fallback_allowed()
    )


def metered_alias_fallback_blocked_message(err: MeteredAliasFallbackBlockedError) -> str:
    return (
        f"Alias '{err.alias}' would switch from your active "
        f"{err.active_provider!r} route (subscription/OAuth/keyless) to metered "
        f"API-key provider {err.refused_provider!r}. Pin the alias in "
        f"config.yaml model_aliases: or set model_aliases_allow_metered_fallback: "
        f"true to allow paid fallback."
    )


def resolve_alias_fallback(
    raw_input: str,
    authenticated_providers: list[str] = (),
    active_provider: str = "",
    *,
    resolve_alias_fn: Callable[[str, str], Optional[tuple[str, str, str]]],
) -> Optional[tuple[str, str, str]]:
    """Resolve an alias across authenticated providers with a metered billing guard."""
    key = raw_input.strip().lower()
    active = (active_provider or "").strip().lower()
    guard_metered = (
        bool(active)
        and active_provider_blocks_metered_fallback(active)
        and not metered_alias_fallback_allowed()
    )

    def _guarded(result: tuple[str, str, str]) -> tuple[str, str, str]:
        if guard_metered and provider_is_metered_api_key(result[0]):
            raise MeteredAliasFallbackBlockedError(key, active, result[0])
        return result

    try:
        from hermes_cli import models as model_catalog
    except Exception:
        model_catalog = None
    if model_catalog is not None:
        policy = model_catalog._registered_alias_policy_result(key)
        if policy is model_catalog._AMBIGUOUS_ALIAS_POLICY:
            raise model_catalog.AmbiguousProviderPolicyError(key)
        if isinstance(policy, tuple):
            provider, model = policy
            return _guarded((provider, model, key))

    providers = authenticated_providers or ("openrouter", "nous")
    refused: tuple[str, str, str] | None = None
    for provider in providers:
        result = resolve_alias_fn(raw_input, provider)
        if result is None:
            continue
        if guard_metered and provider_is_metered_api_key(result[0]):
            refused = result
            continue
        return result
    if refused is not None:
        raise MeteredAliasFallbackBlockedError(
            raw_input.strip().lower(),
            active,
            refused[0],
        )
    return None


def normalize_accumulated_tool_name(
    wire_name: str,
    *,
    agent: Any,
    api_kwargs: Mapping[str, Any] | None = None,
) -> str:
    """Apply the active profile's stream_delta normalizer before callbacks."""
    if not isinstance(wire_name, str) or not wire_name:
        return wire_name
    try:
        from agent.transports.chat_completions import _normalize_response_tool_name
        from providers import get_provider_profile
    except Exception:
        return wire_name
    try:
        profile = get_provider_profile(getattr(agent, "provider", "") or "")
    except Exception:
        profile = None
    aliases = getattr(getattr(agent, "_chat_transport", None), "_last_wire_aliases", None)
    model = None
    if isinstance(api_kwargs, Mapping):
        model = api_kwargs.get("model")
    if not model:
        model = getattr(agent, "model", None)
    return _normalize_response_tool_name(
        wire_name,
        profile=profile,
        phase="stream_delta",
        model=model,
        request_wire_aliases=aliases,
    )


@dataclass(frozen=True)
class SwitchAliasFallbackOutcome:
    result: tuple[str, str, str] | None = None
    error_message: str | None = None


def run_switch_alias_fallback(
    raw_input: str,
    authenticated_providers: list[str],
    active_provider: str,
    *,
    resolve_alias_fn: Callable[[str, str], Optional[tuple[str, str, str]]],
) -> SwitchAliasFallbackOutcome:
    """Resolve alias fallback for switch_model, including metered billing guard."""
    from hermes_cli.model_switch import AmbiguousAliasError

    try:
        result = resolve_alias_fallback(
            raw_input,
            authenticated_providers,
            active_provider,
            resolve_alias_fn=resolve_alias_fn,
        )
    except AmbiguousAliasError:
        raise
    except MeteredAliasFallbackBlockedError as err:
        return SwitchAliasFallbackOutcome(
            error_message=metered_alias_fallback_blocked_message(err),
        )
    return SwitchAliasFallbackOutcome(result=result)
