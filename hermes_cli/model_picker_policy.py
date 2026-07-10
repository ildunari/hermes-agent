"""Root-shared presentation policy for provider/model pickers.

Runtime provider configuration remains profile-scoped.  This module only owns
which configured rows are displayed and the labels used for them.  The shared
file lives outside every named profile so Desktop, WebUI, TUI, and gateway
pickers resolve the same policy without copying it into each ``config.yaml``.
"""

from __future__ import annotations

from copy import deepcopy
import logging
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home

SHARED_MODEL_PICKER_FILENAME = "model-picker.yaml"
logger = logging.getLogger(__name__)
_LAST_GOOD: dict[Path, dict[str, Any]] = {}
_WARNED_ERRORS: set[tuple[str, str]] = set()


def shared_model_picker_path() -> Path:
    """Return the machine-shared picker policy path."""
    home = get_hermes_home()
    root = home.parent.parent if home.parent.name == "profiles" else home
    return root / "shared" / SHARED_MODEL_PICKER_FILENAME


def _mapping(value: Any) -> dict[str, Any]:
    return deepcopy(value) if isinstance(value, dict) else {}


def _validate_string_list_field(name: str, value: Any) -> None:
    if isinstance(value, str):
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a string or list of strings")


def _validate_policy(section: dict[str, Any]) -> None:
    """Validate known policy fields before replacing the last-good snapshot."""
    if "authoritative" in section and not isinstance(section["authoritative"], bool):
        raise ValueError("authoritative must be a boolean")

    for name in (
        "hidden_providers",
        "hide_providers",
        "pinned_providers",
        "show_providers",
    ):
        if name in section:
            _validate_string_list_field(name, section[name])

    for name in ("visible_models", "show_models", "hidden_models", "hide_models"):
        if name not in section:
            continue
        value = section[name]
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be a provider-to-models mapping")
        for provider, models in value.items():
            if not isinstance(provider, str):
                raise ValueError(f"{name} provider keys must be strings")
            _validate_string_list_field(f"{name}.{provider}", models)

    if "provider_labels" in section:
        labels = section["provider_labels"]
        if not isinstance(labels, dict) or not all(
            isinstance(provider, str) and isinstance(label, str)
            for provider, label in labels.items()
        ):
            raise ValueError("provider_labels must map provider strings to label strings")

    if "model_labels" in section:
        labels = section["model_labels"]
        if not isinstance(labels, dict):
            raise ValueError("model_labels must be a nested mapping")
        for provider, model_map in labels.items():
            if not isinstance(provider, str) or not isinstance(model_map, dict):
                raise ValueError("model_labels must map provider strings to mappings")
            if not all(
                isinstance(model_id, str) and isinstance(label, str)
                for model_id, label in model_map.items()
            ):
                raise ValueError(f"model_labels.{provider} must map model IDs to labels")


def load_shared_model_picker_policy(path: Path | None = None) -> dict[str, Any]:
    """Load the root-shared picker policy, returning an empty policy on errors.

    ``path`` is injectable for tests and maintenance scripts.  Production
    callers deliberately use the root-derived default rather than an env var:
    this is behavioral config, not a secret.
    """
    policy_path = path or shared_model_picker_path()
    try:
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("policy root must be a mapping")
        section = raw.get("model_picker", raw)
        if not isinstance(section, dict):
            raise ValueError("model_picker must be a mapping")
        _validate_policy(section)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        warning_key = (str(policy_path), f"{type(exc).__name__}: {exc}")
        if warning_key not in _WARNED_ERRORS:
            _WARNED_ERRORS.add(warning_key)
            logger.warning("Ignoring invalid shared model-picker policy %s: %s", policy_path, exc)
        return _mapping(_LAST_GOOD.get(policy_path))
    policy = _mapping(section)
    _LAST_GOOD[policy_path] = policy
    return _mapping(policy)


def merge_model_picker_policy(
    config: dict | None, shared: dict | None = None
) -> dict[str, Any]:
    """Merge shared policy with legacy profile-local picker sections.

    Shared policy is the base.  Profile-local values remain supported as a
    compatibility override, but normal multi-profile installs should keep the
    policy only in ``<hermes-root>/shared/model-picker.yaml``.
    """
    merged = _mapping(
        shared if shared is not None else load_shared_model_picker_policy()
    )
    if merged.get("authoritative") is True:
        return merged
    for section_name in ("model_catalog", "model_picker"):
        section = (config or {}).get(section_name)
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            if key in {"hidden_providers", "hide_providers"}:
                existing = string_list(
                    merged.get("hidden_providers") or merged.get("hide_providers")
                )
                incoming = string_list(value)
                merged["hidden_providers"] = list(dict.fromkeys((*existing, *incoming)))
                continue
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                nested = _mapping(merged[key])
                nested.update(deepcopy(value))
                merged[key] = nested
            else:
                merged[key] = deepcopy(value)
    return merged


def string_list(value: Any) -> tuple[str, ...]:
    """Normalize comma-separated or sequence config values."""
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return ()
    return tuple(text for item in values if (text := str(item).strip()))


def provider_map(value: Any) -> dict[str, tuple[str, ...]]:
    """Normalize ``provider -> model list`` policy maps."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for provider, models in value.items():
        slug = str(provider or "").strip().lower()
        normalized = string_list(models)
        if slug and normalized:
            out[slug] = normalized
    return out


def label_map(value: Any) -> dict[str, str]:
    """Normalize a simple id-to-display-label mapping."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, label in value.items():
        normalized_key = str(key or "").strip().lower()
        normalized_label = str(label or "").strip()
        if normalized_key and normalized_label:
            out[normalized_key] = normalized_label
    return out


def nested_label_map(value: Any) -> dict[str, dict[str, str]]:
    """Normalize ``provider -> model id -> display label`` mappings.

    Provider slugs are case-insensitive; model IDs retain their exact spelling
    because some custom endpoints use case-sensitive route IDs.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for provider, labels in value.items():
        slug = str(provider or "").strip().lower()
        if not slug or not isinstance(labels, dict):
            continue
        normalized: dict[str, str] = {}
        for model_id, label in labels.items():
            route_id = str(model_id or "").strip()
            display_label = str(label or "").strip()
            if route_id and display_label:
                normalized[route_id] = display_label
        if normalized:
            out[slug] = normalized
    return out
