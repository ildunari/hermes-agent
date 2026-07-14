"""Text-free proactive health and dead-system diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from gateway.proactive_scheduler import ProactiveConfig

_REQUIRED_MODEL = {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium"}


def _lane_ok(config: Mapping[str, Any], task: str) -> bool:
    lane = (config.get("auxiliary", {}) or {}).get(task, {})
    return isinstance(lane, Mapping) and all(lane.get(k) == v for k, v in _REQUIRED_MODEL.items()) and lane.get("fallback") is False


def health_snapshot(*, profile_home: str | Path, profile: str, config: Mapping[str, Any],
                    now: float | None = None, adapter_ready: bool | None = None,
                    cron_fresh: bool | None = None) -> dict[str, Any]:
    timestamp = float(time.time() if now is None else now)
    root = Path(profile_home).expanduser().resolve()
    proactive_raw = (config.get("agent", {}) or {}).get("proactive", {})
    cfg = ProactiveConfig.from_mapping(proactive_raw if isinstance(proactive_raw, Mapping) else {})
    compose = proactive_raw.get("compose_model", {}) if isinstance(proactive_raw, Mapping) else {}
    compose_ok = isinstance(compose, Mapping) and compose.get("provider") == "openai-codex" and compose.get("model") == "gpt-5.6-sol" and compose.get("reasoning_effort") == "low" and compose.get("fallback") is False
    result: dict[str, Any] = {
        "profile": profile, "enabled": cfg.enabled, "mode": cfg.mode.value,
        "model_lane_match": _lane_ok(config, "proactive_gate") and _lane_ok(config, "proactive_semantic") and compose_ok,
        "adapter_ready": adapter_ready, "cron_fresh": cron_fresh,
        "contacts": 0, "recent_inbound": 0, "slots": {}, "deliveries": {},
        "attempts": 0, "last_success_at": None, "last_error_class": None,
        "watcher_heartbeat_at": None, "watcher_age_seconds": None,
        "dead": False, "reasons": [],
    }
    db = root / "state.db"
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "proactive_contact" in tables:
                result["contacts"] = int(con.execute("SELECT count(*) FROM proactive_contact").fetchone()[0])
                result["recent_inbound"] = int(con.execute("SELECT count(*) FROM proactive_inbound WHERE received_at>=?", (timestamp - 14*86400,)).fetchone()[0])
                result["slots"] = {str(r[0]): int(r[1]) for r in con.execute("SELECT status,count(*) FROM proactive_slot GROUP BY status")}
                stale = con.execute("SELECT count(*) FROM proactive_slot WHERE status='claimed' AND claim_until<?", (timestamp,)).fetchone()[0]
                if stale: result["reasons"].append("stale_claim_lease")
            if "proactive_delivery" in tables:
                rows = con.execute("SELECT state,count(*) n FROM proactive_delivery GROUP BY state").fetchall()
                result["deliveries"] = {str(r[0]): int(r[1]) for r in rows}
                result["attempts"] = int(con.execute("SELECT COALESCE(sum(attempt_count),0) FROM proactive_delivery").fetchone()[0])
                last = con.execute("SELECT state,last_error_class,updated_at FROM proactive_delivery ORDER BY updated_at DESC LIMIT 1").fetchone()
                if last:
                    if last["state"] == "sent": result["last_success_at"] = float(last["updated_at"])
                    result["last_error_class"] = last["last_error_class"]
                if result["deliveries"].get("failed", 0): result["reasons"].append("transport_failure_exhaustion")
            if "proactive_health" in tables:
                heartbeat = con.execute("SELECT updated_at FROM proactive_health WHERE key='watcher' LIMIT 1").fetchone()
                if heartbeat:
                    result["watcher_heartbeat_at"] = float(heartbeat[0])
                    result["watcher_age_seconds"] = max(0.0, timestamp - float(heartbeat[0]))
            con.close()
        except sqlite3.Error:
            result["reasons"].append("state_db_unreadable")
    elif cfg.enabled:
        result["reasons"].append("state_db_missing")
    if cfg.enabled and (result["watcher_age_seconds"] is None or result["watcher_age_seconds"] > 65 * 60):
        result["reasons"].append("watcher_stale")
    if cfg.enabled and not result["model_lane_match"]:
        result["reasons"].append("model_lane_mismatch")
    if cfg.enabled and adapter_ready is False:
        result["reasons"].append("adapter_unavailable")
    if cfg.enabled and cron_fresh is False:
        result["reasons"].append("maintenance_cron_stale")
    if cfg.mode.value == "live" and not cfg.allowed_contacts:
        result["reasons"].append("allowlist_missing")
    result["reasons"] = sorted(set(result["reasons"]))
    result["dead"] = bool(result["reasons"])
    return result


__all__ = ["health_snapshot"]
