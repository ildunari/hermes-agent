"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its
    order. Legacy ``fallback_model`` entries are appended afterwards unless
    they target the same provider/model/base_url route as an earlier entry.
    The returned list always contains fresh dict copies.
    """

    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(entry)

    return chain


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON next to *path*, preserving basic file mode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    mode: int | None = None
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        if mode is not None:
            try:
                os.chmod(tmp_path, mode)
            except OSError:
                pass
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _refresh_codex_home_tokens(auth_path: Path, data: dict[str, Any], tokens: dict[str, Any]) -> str | None:
    """Refresh and persist a Codex CLI home token payload, if possible."""

    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        return access_token or None
    try:
        from hermes_cli.auth import (
            CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
            _codex_access_token_is_expiring,
            refresh_codex_oauth_pure,
        )
    except Exception:
        return access_token or None
    try:
        should_refresh = not access_token or _codex_access_token_is_expiring(
            access_token,
            CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
        )
    except Exception:
        should_refresh = True
    if not should_refresh:
        return access_token or None
    try:
        refreshed = refresh_codex_oauth_pure(access_token, refresh_token)
    except Exception:
        return None if should_refresh else (access_token or None)
    refreshed_access = str(refreshed.get("access_token") or "").strip()
    if not refreshed_access:
        return access_token or None
    updated_tokens = dict(tokens)
    updated_tokens["access_token"] = refreshed_access
    refreshed_refresh = str(refreshed.get("refresh_token") or "").strip()
    if refreshed_refresh:
        updated_tokens["refresh_token"] = refreshed_refresh
    updated_data = dict(data)
    updated_data["tokens"] = updated_tokens
    if refreshed.get("last_refresh"):
        updated_data["last_refresh"] = refreshed["last_refresh"]
    try:
        _atomic_write_json(auth_path, updated_data)
    except Exception:
        # The fresh access token is still usable for this process even if we
        # could not persist it; the next run may need to refresh again.
        pass
    return refreshed_access


def codex_home_access_token(entry: dict[str, Any] | None) -> str | None:
    """Return a Codex CLI access token for an openai-codex fallback entry.

    Hermes' normal ``openai-codex`` auth lives in the active Hermes
    ``auth.json``. A fallback entry can opt into a specific Codex CLI account
    with ``codex_home: /path/to/.codex-home``; this reads that home's
    ``auth.json`` so the fallback uses the requested account instead of the
    active Hermes profile's Codex OAuth state.  Alternate Codex homes carry a
    refresh token too, so refresh and persist an expiring access token instead
    of letting the fallback immediately 401 and skip to the next provider.
    """

    if not isinstance(entry, dict):
        return None
    provider = str(entry.get("provider") or "").strip().lower()
    if provider not in {"openai-codex", "codex"}:
        return None
    raw_home = str(entry.get("codex_home") or "").strip()
    if not raw_home:
        return None
    auth_path = Path(raw_home).expanduser() / "auth.json"
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    token = _refresh_codex_home_tokens(auth_path, data, tokens)
    return token or None
