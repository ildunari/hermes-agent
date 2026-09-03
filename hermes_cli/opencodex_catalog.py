"""OpenCodex model-catalog routing for Codex app-server profiles.

OpenCodex exposes several vendors through one OpenAI-compatible Codex
provider.  Hermes must therefore keep its runtime provider on
``openai-codex`` and forward the catalog's canonical model slug verbatim;
switching Hermes itself to ``xai-oauth``/``anthropic`` bypasses app-server and
looks for unrelated Hermes credentials.
"""

from __future__ import annotations

import difflib
import json
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class OpenCodexCatalogError(RuntimeError):
    """A profile declared an OpenCodex catalog that cannot be used safely."""


@dataclass(frozen=True)
class OpenCodexModel:
    slug: str
    display_name: str
    context_window: int = 0
    effective_context_window_percent: int = 100

    @property
    def effective_context_window(self) -> int:
        if self.context_window <= 0:
            return 0
        percent = min(max(self.effective_context_window_percent, 1), 100)
        return self.context_window * percent // 100


@dataclass(frozen=True)
class OpenCodexMatch:
    model: OpenCodexModel | None
    error: str = ""


@dataclass(frozen=True)
class OpenCodexCatalog:
    path: Path
    models: tuple[OpenCodexModel, ...]

    def resolve(self, supplied: str, *, provider_hint: str = "") -> OpenCodexMatch:
        raw = str(supplied or "").strip()
        if not raw:
            return OpenCodexMatch(None, "An OpenCodex model is required.")

        folded = raw.casefold()
        exact = [
            item
            for item in self.models
            if folded in {item.slug.casefold(), item.display_name.casefold()}
        ]
        if len(exact) == 1:
            return OpenCodexMatch(exact[0])

        key = _model_key(raw)
        candidates: list[OpenCodexModel] = []
        for item in self.models:
            aliases = {
                _model_key(item.slug),
                _model_key(item.display_name),
                _model_key(_model_leaf(item.slug)),
            }
            if key and key in aliases:
                candidates.append(item)

        unique = {item.slug: item for item in candidates}
        vendor_hint = _provider_vendor_hint(provider_hint)
        if len(unique) > 1 and vendor_hint:
            hinted = {
                slug: item
                for slug, item in unique.items()
                if _model_vendor(slug) == vendor_hint
            }
            if len(hinted) == 1:
                return OpenCodexMatch(next(iter(hinted.values())))
        if len(unique) == 1:
            return OpenCodexMatch(next(iter(unique.values())))
        if len(unique) > 1:
            choices = ", ".join(sorted(unique))
            return OpenCodexMatch(
                None,
                f"Model name {raw!r} is ambiguous in the OpenCodex catalog: {choices}. "
                "Choose the full model id.",
            )

        labels = [item.slug for item in self.models]
        comparison = {
            _model_key(item.slug): item.slug for item in self.models
        } | {
            _model_key(item.display_name): item.slug for item in self.models
        }
        close_keys = difflib.get_close_matches(key, comparison, n=3, cutoff=0.55)
        suggestions = [comparison[value] for value in close_keys]
        visible = suggestions or labels
        suffix = ", ".join(dict.fromkeys(visible))
        return OpenCodexMatch(
            None,
            f"Model {raw!r} is not in the OpenCodex catalog. Available matches: {suffix}",
        )

    @property
    def model_labels(self) -> dict[str, str]:
        return {item.slug: item.display_name for item in self.models}


def load_configured_opencodex_catalog(
    config: dict[str, Any] | None = None,
) -> OpenCodexCatalog | None:
    """Load the catalog selected by a Codex app-server profile.

    Generic stock-Codex app-server profiles are unchanged.  The special picker
    activates only when the selected Codex home/config explicitly declares a
    ``model_catalog_json`` path (OpenCodex's supported catalog contract).
    """
    if config is None:
        from hermes_cli.config import load_config

        config = load_config() or {}
    if not isinstance(config, dict):
        return None
    model_cfg = config.get("model")
    if not isinstance(model_cfg, dict):
        return None
    if str(model_cfg.get("openai_runtime") or "").strip().lower() != "codex_app_server":
        return None
    runtime_cfg = model_cfg.get("codex_app_server")
    if not isinstance(runtime_cfg, dict):
        return None

    raw_path = str(runtime_cfg.get("model_catalog_json") or "").strip()
    codex_home = str(runtime_cfg.get("codex_home") or "").strip()
    if not raw_path and codex_home:
        config_path = Path(codex_home).expanduser() / "config.toml"
        try:
            with config_path.open("rb") as handle:
                raw_path = str(tomllib.load(handle).get("model_catalog_json") or "").strip()
        except (OSError, tomllib.TOMLDecodeError):
            return None
    if not raw_path:
        return None

    path = Path(raw_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenCodexCatalogError(
            f"OpenCodex catalog is unavailable or invalid: {path}"
        ) from exc
    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raise OpenCodexCatalogError(
            f"OpenCodex catalog has no models list: {path}"
        )

    models: list[OpenCodexModel] = []
    seen: set[str] = set()
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or entry.get("id") or "").strip()
        if not slug or slug.casefold() in seen:
            continue
        seen.add(slug.casefold())
        display = str(entry.get("display_name") or entry.get("name") or slug).strip()
        context_window = _positive_int(entry.get("context_window"))
        effective_percent = _positive_int(
            entry.get("effective_context_window_percent")
        )
        models.append(
            OpenCodexModel(
                slug=slug,
                display_name=display or slug,
                context_window=context_window,
                effective_context_window_percent=effective_percent or 100,
            )
        )
    if not models:
        raise OpenCodexCatalogError(f"OpenCodex catalog contains no models: {path}")
    return OpenCodexCatalog(path=path, models=tuple(models))


def opencodex_runtime_provider(config: dict[str, Any] | None = None) -> str:
    """Return the Hermes provider that keeps the app-server gate active."""
    if config is None:
        from hermes_cli.config import load_config

        config = load_config() or {}
    model_cfg = config.get("model") if isinstance(config, dict) else None
    configured = (
        str(model_cfg.get("provider") or "").strip().lower()
        if isinstance(model_cfg, dict)
        else ""
    )
    return configured if configured in {"openai", "openai-codex"} else "openai-codex"


def _model_leaf(slug: str) -> str:
    return re.split(r"[/:]", slug)[-1]


def _model_vendor(slug: str) -> str:
    parts = re.split(r"[/:]", slug, maxsplit=1)
    return _model_key(parts[0]) if len(parts) > 1 else "openai"


def _provider_vendor_hint(provider: str) -> str:
    key = _model_key(provider)
    aliases = {
        "xaioauth": "xai",
        "xai": "xai",
        "anthropic": "anthropic",
        "kimi": "moonshot",
        "kimicoding": "moonshot",
        "moonshot": "moonshot",
        "moonshotoauth": "moonshot",
    }
    return aliases.get(key, "")


def _model_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Vendor spellings vary between Hermes and OpenCodex (x-ai/xai, moonshot
    # vs moonshotai). Canonicalize only known spelling mutations, then remove
    # punctuation. Exact and unique matching above prevents fuzzy guessing.
    normalized = normalized.replace("x-ai", "xai").replace("moonshotai", "moonshot")
    return "".join(ch for ch in normalized if ch.isalnum())


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
