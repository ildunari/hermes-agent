"""Fail-closed profile delegation for proactive cron alarm delivery."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml

from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
from gateway.config import Platform, load_gateway_config
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_RECENT_ROUTE_AGE = timedelta(days=30)


def resolve_delivery_profile_home(source_home: Path, profile_name: str) -> Path:
    """Resolve one named sibling profile without accepting paths or symlinks."""
    name = str(profile_name or "").strip()
    if not _PROFILE_NAME.fullmatch(name) or name in {".", "..", "guest"}:
        raise ValueError("delivery_profile must name an existing operator profile")
    source = Path(source_home).resolve()
    profiles_root = source.parent
    candidate = profiles_root / name
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError(f"delivery profile {name!r} does not exist at its canonical profile path")
    resolved = candidate.resolve()
    if resolved.parent != profiles_root or resolved.name != name:
        raise ValueError("delivery_profile escaped the canonical profiles directory")
    return resolved


def _explicit_platform_block(home: Path, platform: str) -> dict[str, Any] | None:
    try:
        raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("delivery profile config is unavailable") from exc
    blocks = (
        ((raw.get("gateway") or {}).get("platforms") or {}).get(platform),
        (raw.get("platforms") or {}).get(platform),
        raw.get(platform),
    )
    return next((block for block in blocks if isinstance(block, dict)), None)


def load_delivery_profile_config(home: Path, platform_name: str):
    """Load config using only the selected profile's isolated secret scope."""
    try:
        platform = Platform(platform_name)
    except ValueError as exc:
        raise ValueError(f"unsupported delegated delivery platform {platform_name!r}") from exc
    block = _explicit_platform_block(home, platform_name)
    if not block or block.get("enabled") is not True:
        raise ValueError(f"{platform_name} is not explicitly enabled for delivery profile {home.name}")
    home_token = set_hermes_home_override(home)
    secret_token = set_secret_scope(build_profile_secret_scope(home))
    try:
        config = load_gateway_config()
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)
    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        raise ValueError(f"{platform_name} is not configured/enabled for delivery profile {home.name}")
    if platform in {Platform.TELEGRAM, Platform.DISCORD, Platform.SLACK, Platform.MATRIX}:
        if not str(pconfig.token or "").strip():
            raise ValueError(f"{platform_name} outbound credentials are incomplete for delivery profile {home.name}")
    elif platform == Platform.BLUEBUBBLES:
        if not str(pconfig.extra.get("server_url") or "").strip() or not str(pconfig.extra.get("password") or "").strip():
            raise ValueError("BlueBubbles outbound credentials are incomplete")
    return config, platform, pconfig


def _routing_entries(home: Path) -> Iterable[dict[str, Any]]:
    seen: set[str] = set()
    db_path = home / "state.db"
    if db_path.is_file():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                for (payload,) in conn.execute("SELECT entry_json FROM gateway_routing"):
                    if payload in seen:
                        continue
                    seen.add(payload)
                    value = json.loads(payload)
                    if isinstance(value, dict):
                        yield value
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
    mirror = home / "sessions" / "sessions.json"
    try:
        values = json.loads(mirror.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if isinstance(values, dict):
        for value in values.values():
            if isinstance(value, dict):
                yield value


def _recent_authenticated_dm(home: Path, platform: str, target: str) -> bool:
    cutoff = datetime.now(timezone.utc) - _RECENT_ROUTE_AGE
    for entry in _routing_entries(home):
        raw_origin = entry.get("origin")
        origin: dict[str, Any] = raw_origin if isinstance(raw_origin, dict) else {}
        if str(origin.get("platform") or entry.get("platform") or "").lower() != platform:
            continue
        chat_id = str(origin.get("chat_id") or "")
        chat_type = str(origin.get("chat_type") or entry.get("chat_type") or "dm").lower()
        user_id = str(origin.get("user_id") or "")
        if chat_id != target or chat_type not in {"dm", "private"} or not user_id:
            continue
        try:
            updated = datetime.fromisoformat(str(entry.get("updated_at") or "").replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if updated >= cutoff:
            return True
    return False


def _allowed_chat(pconfig: Any, target: str) -> bool:
    for key in ("allowed_chats", "group_allowed_chats"):
        raw = pconfig.extra.get(key)
        if isinstance(raw, str):
            values = [part.strip() for part in raw.split(",")]
        elif isinstance(raw, (list, tuple, set)):
            values = [str(part).strip() for part in raw]
        else:
            continue
        if target in values:
            return True
    return False


def validate_delegated_alarm_delivery(source_home: Path, delivery_profile: str, target: dict[str, str]):
    """Validate profile, platform, credentials, and an operator-owned destination."""
    home = resolve_delivery_profile_home(source_home, delivery_profile)
    config, platform, pconfig = load_delivery_profile_config(home, target["platform"])
    address = target["address"]

    # Internal proactive alarms must never enter a contact conversation.  Poke
    # owns BlueBubbles ingress, but that ownership is not permission for cron,
    # maintenance, watchdog, bootstrap, probe, or dry-run output to use the
    # transport.  Operator alarms must use an explicitly authenticated
    # non-contact surface (normally Telegram).
    if platform == Platform.BLUEBUBBLES:
        raise ValueError("internal proactive alarm delivery to BlueBubbles is forbidden")
    elif not (_recent_authenticated_dm(home, target["platform"], address) or _allowed_chat(pconfig, address)):
        raise ValueError(
            "alarm destination must match an authenticated recent DM or explicitly allowed chat "
            f"for delivery profile {home.name}"
        )
    return home, config, platform, pconfig
