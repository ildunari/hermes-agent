"""Text-free proactive health and dead-system diagnostics."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from typing import Any, Mapping

from gateway.proactive_scheduler import ProactiveConfig, ProactiveMode

_REQUIRED_MODEL = {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium"}


def probe_model_readiness(*, now: float | None = None, resolver: Any = None,
                          timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Issue one tiny strict-lane request with no conversation/private history."""
    timestamp = float(time.time() if now is None else now)
    if resolver is None:
        from agent.auxiliary_client import resolve_provider_client
        resolver = resolve_provider_client
    result: dict[str, Any] = {
        "checked_at": timestamp, "ready": False, "provider": "openai-codex",
        "requested_model": "gpt-5.6-sol", "resolved_model": None,
        "response_model": None, "sent_request": False, "max_completion_tokens": 4,
        "timeout_seconds": float(timeout_seconds), "private_history_used": False,
    }
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="proactive-model-probe")
    try:
        client, model = resolver(
            "openai-codex", model="gpt-5.6-sol", task="proactive_gate",
            api_mode="codex_responses",
        )
        result["resolved_model"] = model
        create = getattr(getattr(getattr(client, "chat", None), "completions", None), "create", None)
        if client is None or model != "gpt-5.6-sol" or not callable(create):
            result["error_class"] = "StrictRouteResolutionError"
            return result
        future = executor.submit(
            create,
            model="gpt-5.6-sol",
            messages=[{"role": "user", "content": "Reply exactly OK."}],
            max_completion_tokens=4,
            timeout=float(timeout_seconds),
            extra_body={"reasoning": {"effort": "low"}},
        )
        result["sent_request"] = True
        response = future.result(timeout=float(timeout_seconds) + 1.0)
        response_model = str(getattr(response, "model", "") or "")
        result["response_model"] = response_model
        usage = getattr(response, "usage", None)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        result["completion_tokens"] = completion_tokens
        result["ready"] = bool(response_model == "gpt-5.6-sol" and completion_tokens <= 64)
        if not result["ready"]:
            result["error_class"] = "ResolvedRouteVerificationError"
    except FutureTimeout:
        result["error_class"] = "ProbeTimeout"
    except Exception as exc:
        result["error_class"] = type(exc).__name__
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return result


def _lane_ok(config: Mapping[str, Any], task: str) -> bool:
    lane = (config.get("auxiliary", {}) or {}).get(task, {})
    return isinstance(lane, Mapping) and all(lane.get(k) == v for k, v in _REQUIRED_MODEL.items()) and lane.get("fallback") is False


def probe_alarm_sink_readiness(*, profile_home: str | Path, config: ProactiveConfig,
                               now: float | None = None) -> dict[str, Any]:
    """Verify installed cron target plus a recent transport delivery ACK."""
    timestamp = float(time.time() if now is None else now)
    root = Path(profile_home).expanduser().resolve()
    result = {"checked_at": timestamp, "ready": False, "delivery_ack": False,
              "type": config.alarm_sink_type, "target": config.alarm_sink_target}
    if not config.alarm_sink_configured:
        result["error_class"] = "AlarmSinkConfigError"
        return result
    try:
        from cron.jobs import list_jobs, use_cron_store
        from scripts.install_proactive_rollout_cron import (
            ALARM_PROBE_NAME, ALARM_PROBE_SCRIPT, WATCHDOG_NAME, WATCHDOG_SCRIPT,
        )
        manifest = json.loads((root / "proactive-alarm-sink.json").read_text(encoding="utf-8"))
        nonce = str(manifest.get("nonce") or "")
        generation = str(manifest.get("generation") or "")
        binding = manifest.get("binding")
        script = (root / "scripts" / ALARM_PROBE_SCRIPT).read_text(encoding="utf-8")
        with use_cron_store(root):
            all_jobs = list_jobs(include_disabled=True)
            jobs = [job for job in all_jobs if job.get("name") == ALARM_PROBE_NAME]
            watchdogs = [job for job in all_jobs if job.get("name") == WATCHDOG_NAME]
        if len(jobs) != 1:
            raise RuntimeError("alarm probe job count mismatch")
        if len(watchdogs) != 1:
            raise RuntimeError("alarm watchdog job count mismatch")
        job = jobs[0]
        watchdog = watchdogs[0]
        structural = bool(
            manifest.get("version") == 2
            and manifest.get("type") == "hermes_cron"
            and manifest.get("target") == config.alarm_sink_target
            and isinstance(binding, dict)
            and job.get("probe_binding") == binding
            and job.get("enabled", True) and job.get("no_agent") is True
            and job.get("script") == ALARM_PROBE_SCRIPT
            and job.get("deliver") == config.alarm_sink_target
            and nonce and generation
            and binding.get("nonce") == nonce
            and binding.get("generation") == generation
            and binding.get("target") == config.alarm_sink_target
            and binding.get("script") == ALARM_PROBE_SCRIPT
            and binding.get("script_sha256") == hashlib.sha256(script.encode("utf-8")).hexdigest()
            and f"{nonce} {generation}" in script
            and watchdog.get("enabled", True) and watchdog.get("no_agent") is True
            and watchdog.get("script") == WATCHDOG_SCRIPT
            and watchdog.get("deliver") == config.alarm_sink_target
        )
        last_run = job.get("last_run_at")
        run_at = datetime.fromisoformat(str(last_run)).timestamp() if last_run else None
        fresh = bool(run_at is not None and 0 <= timestamp - run_at <= config.alarm_probe_max_age_seconds)
        ack_metadata = job.get("last_probe_delivery_ack")
        expected_ack = {**binding, "run_at": str(last_run)} if isinstance(binding, dict) else None
        ack = bool(
            job.get("last_status") == "ok" and not job.get("last_delivery_error")
            and isinstance(ack_metadata, dict) and ack_metadata == expected_ack
        )
        result.update({"structural": structural, "last_ack_at": run_at,
                       "ack_age_seconds": timestamp - run_at if run_at is not None else None,
                       "delivery_ack": ack and fresh, "ready": structural and ack and fresh})
    except Exception as exc:
        result["error_class"] = type(exc).__name__
    return result


def health_snapshot(*, profile_home: str | Path, profile: str, config: Mapping[str, Any],
                    now: float | None = None, adapter_ready: bool | None = None,
                    cron_fresh: bool | None = None) -> dict[str, Any]:
    timestamp = float(time.time() if now is None else now)
    root = Path(profile_home).expanduser().resolve()
    proactive_raw = (config.get("agent", {}) or {}).get("proactive", {})
    config_error = None
    try:
        cfg = ProactiveConfig.from_mapping(proactive_raw if isinstance(proactive_raw, Mapping) else {})
    except (TypeError, ValueError) as exc:
        config_error = type(exc).__name__
        cfg = ProactiveConfig(
            enabled=bool(isinstance(proactive_raw, Mapping) and proactive_raw.get("enabled")),
            mode=ProactiveMode.DISABLED,
        )
    compose = proactive_raw.get("compose_model", {}) if isinstance(proactive_raw, Mapping) else {}
    compose_ok = isinstance(compose, Mapping) and compose.get("provider") == "openai-codex" and compose.get("model") == "gpt-5.6-sol" and compose.get("reasoning_effort") == "low" and compose.get("fallback") is False
    result: dict[str, Any] = {
        "profile": profile, "enabled": cfg.enabled, "mode": cfg.mode.value,
        "model_lane_match": _lane_ok(config, "proactive_gate") and _lane_ok(config, "proactive_semantic") and compose_ok,
        "model_probe": None, "model_probe_age_seconds": None,
        "alarm_sink_configured": cfg.alarm_sink_configured, "alarm_probe": None,
        "send_rate": 0.0, "send_rate_total": 0, "send_rate_sent": 0,
        "adapter_ready": adapter_ready, "cron_fresh": cron_fresh,
        "participant_registry_ready": None,
        "ownership_conflict": False, "global_circuit": "unknown",
        "eligible_interests": 0, "interest_count": 0, "unfolded_interest_events": 0,
        "digest_count": 0, "oldest_digest_age_seconds": None,
        "contacts": 0, "recent_inbound": 0, "observed_ingress": 0,
        "slots": {}, "deliveries": {}, "actions": {}, "outcomes": {},
        "attempts": 0, "retries": 0, "last_success_at": None, "last_error_class": None,
        "oldest_claim_age_seconds": None, "extraction": None,
        "watcher_heartbeat_at": None, "watcher_age_seconds": None,
        "planning_attempt_at": None, "planning_attempt_age_seconds": None,
        "dead": False, "reasons": [],
    }
    if config_error:
        result["reasons"].append("proactive_config_invalid")
    db = root / "state.db"
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "proactive_contact" in tables:
                result["recent_inbound"] = int(con.execute("SELECT count(*) FROM proactive_inbound WHERE received_at>=?", (timestamp - 14*86400,)).fetchone()[0])
                result["slots"] = {str(r[0]): int(r[1]) for r in con.execute("SELECT status,count(*) FROM proactive_slot GROUP BY status")}
                stale = con.execute("SELECT count(*) FROM proactive_slot WHERE status='claimed' AND claim_until<?", (timestamp,)).fetchone()[0]
                if stale: result["reasons"].append("stale_claim_lease")
                claim = con.execute("SELECT min(updated_at) FROM proactive_slot WHERE status='claimed'").fetchone()[0]
                if claim is not None:
                    result["oldest_claim_age_seconds"] = max(0.0, timestamp - float(claim))
            if "proactive_ingress_observed" in tables:
                result["observed_ingress"] = int(con.execute(
                    "SELECT count(*) FROM proactive_ingress_observed WHERE observed_at>=?",
                    (timestamp - 14*86400,),
                ).fetchone()[0])
                if result["observed_ingress"] > result["recent_inbound"]:
                    result["reasons"].append("inbound_tracking_drift")
            if "proactive_delivery" in tables:
                rows = con.execute("SELECT state,count(*) n FROM proactive_delivery GROUP BY state").fetchall()
                result["deliveries"] = {str(r[0]): int(r[1]) for r in rows}
                result["attempts"] = int(con.execute("SELECT COALESCE(sum(attempt_count),0) FROM proactive_delivery").fetchone()[0])
                deliveries_with_attempts = int(con.execute("SELECT count(*) FROM proactive_delivery WHERE attempt_count>0").fetchone()[0])
                result["retries"] = max(0, result["attempts"] - deliveries_with_attempts)
                last = con.execute("SELECT state,last_error_class,updated_at FROM proactive_delivery ORDER BY updated_at DESC LIMIT 1").fetchone()
                if last:
                    if last["state"] == "sent": result["last_success_at"] = float(last["updated_at"])
                    result["last_error_class"] = last["last_error_class"]
                if result["deliveries"].get("failed", 0): result["reasons"].append("transport_failure_exhaustion")
                projection_failures = int(con.execute(
                    "SELECT count(*) FROM proactive_delivery WHERE state='sent' AND projection_state!='complete'"
                ).fetchone()[0])
                if projection_failures:
                    result["reasons"].append("confirmed_send_projection_incomplete")
            if "proactive_action" in tables:
                result["actions"] = {str(r[0]): int(r[1]) for r in con.execute(
                    "SELECT status,count(*) FROM proactive_action GROUP BY status"
                )}
                result["outcomes"] = {str(r[0]): int(r[1]) for r in con.execute(
                    "SELECT outcome,count(*) FROM proactive_action WHERE outcome IS NOT NULL GROUP BY outcome"
                )}
                canonical = con.execute(
                    """SELECT count(*),COALESCE(sum(CASE WHEN status='sent' THEN 1 ELSE 0 END),0)
                       FROM proactive_action WHERE created_at>=?""", (timestamp - 7 * 86400,)
                ).fetchone()
                result["send_rate_total"] = int(canonical[0])
                result["send_rate_sent"] = int(canonical[1])
            if "proactive_health" in tables:
                heartbeat = con.execute("SELECT value_json,updated_at FROM proactive_health WHERE key='watcher' LIMIT 1").fetchone()
                if heartbeat:
                    health = json.loads(heartbeat["value_json"] or "{}")
                    result["watcher_heartbeat_at"] = float(heartbeat["updated_at"])
                    result["watcher_age_seconds"] = max(0.0, timestamp - float(heartbeat["updated_at"]))
                    if result["adapter_ready"] is None:
                        result["adapter_ready"] = health.get("adapter_ready")
                    result["participant_registry_ready"] = health.get("participant_registry_ready")
                    result["extraction"] = health.get("extraction")
                    result["model_probe"] = health.get("model_probe")
                    probe = result["model_probe"]
                    if isinstance(probe, Mapping) and probe.get("checked_at") is not None:
                        result["model_probe_age_seconds"] = max(0.0, timestamp - float(probe["checked_at"]))
                model_probe_row = con.execute("SELECT value_json,updated_at FROM proactive_health WHERE key='model_probe'").fetchone()
                if model_probe_row:
                    result["model_probe"] = json.loads(model_probe_row["value_json"] or "{}")
                    result["model_probe_age_seconds"] = max(0.0, timestamp - float(model_probe_row["updated_at"]))
                alarm_probe_row = con.execute("SELECT value_json,updated_at FROM proactive_health WHERE key='alarm_sink_probe'").fetchone()
                if alarm_probe_row:
                    result["alarm_probe"] = json.loads(alarm_probe_row["value_json"] or "{}")
                planning = con.execute("SELECT value_json,updated_at FROM proactive_health WHERE key='planning_attempt' LIMIT 1").fetchone()
                if planning:
                    result["planning_attempt_at"] = float(planning["updated_at"])
                    result["planning_attempt_age_seconds"] = max(0.0, timestamp - float(planning["updated_at"]))
            con.close()
        except sqlite3.Error:
            result["reasons"].append("state_db_unreadable")
    elif cfg.enabled:
        result["reasons"].append("state_db_missing")
    ownership_db = (root.parent.parent / "proactive-contact-ownership.db"
                    if root.parent.name == "profiles"
                    else root / "proactive-contact-ownership.db")
    if ownership_db.is_file():
        try:
            ownership = sqlite3.connect(f"file:{ownership_db.as_posix()}?mode=ro", uri=True)
            ownership.row_factory = sqlite3.Row
            circuit = ownership.execute("SELECT state,reason FROM proactive_global_circuit WHERE singleton=1").fetchone()
            result["global_circuit"] = str(circuit["state"]) if circuit else "closed"
            if circuit and circuit["state"] == "open":
                result["reasons"].append("global_circuit_open")
            conflicts = ownership.execute("SELECT count(*) FROM proactive_contact_owner GROUP BY contact_hash HAVING count(DISTINCT profile_name)>1").fetchall()
            result["ownership_conflict"] = bool(conflicts)
            if conflicts:
                result["reasons"].append("ownership_conflict")
            ownership.close()
        except sqlite3.Error:
            result["reasons"].append("ownership_registry_unreadable")
    elif cfg.enabled:
        result["reasons"].append("ownership_registry_missing")
    memory_root = root / "contact-memory"
    contact_dir = memory_root / "contacts"
    digest_dir = memory_root / "digests"
    digest_ages: list[float] = []
    missing_eligible_digest = False
    for contact_db in contact_dir.glob("*.sqlite3") if contact_dir.is_dir() else ():
        result["contacts"] += 1
        try:
            memory = sqlite3.connect(f"file:{contact_db.as_posix()}?mode=ro", uri=True)
            result["interest_count"] += int(memory.execute("SELECT count(*) FROM interest").fetchone()[0])
            # The state DB is canonical for confirmed transport. Contact-memory
            # send rows are a projection and are not used for safety accounting.
            result["unfolded_interest_events"] += int(memory.execute(
                "SELECT count(*) FROM interest_event WHERE folded_at IS NULL"
            ).fetchone()[0])
            eligible = 0
            for row in memory.execute(
                "SELECT raw_score,last_evidence_at,half_life_days FROM interest WHERE state='active' AND valence='positive' AND retired_at IS NULL"
            ):
                elapsed = max(0.0, timestamp - float(row[1]))
                score = float(row[0]) * math.pow(0.5, elapsed / (float(row[2]) * 86400.0))
                eligible += int(score >= 2.0)
            result["eligible_interests"] += eligible
            digest = digest_dir / f"{contact_db.stem}.md"
            if digest.is_file():
                result["digest_count"] += 1
                digest_ages.append(max(0.0, timestamp - digest.stat().st_mtime))
            elif eligible:
                missing_eligible_digest = True
            memory.close()
        except sqlite3.Error:
            result["reasons"].append("contact_memory_unreadable")
    if digest_ages:
        result["oldest_digest_age_seconds"] = max(digest_ages)
    extraction = result["extraction"]
    if cfg.enabled and isinstance(extraction, Mapping):
        if int(extraction.get("dead_workers") or 0):
            result["reasons"].append("extraction_worker_dead")
        if bool(extraction.get("queue_full")):
            result["reasons"].append("extraction_queue_full")
    if cfg.enabled and (result["watcher_age_seconds"] is None or result["watcher_age_seconds"] > 65 * 60):
        result["reasons"].append("watcher_stale")
    if cfg.enabled and not result["model_lane_match"]:
        result["reasons"].append("model_lane_mismatch")
    probe = result["model_probe"]
    if cfg.enabled and (
        not isinstance(probe, Mapping) or probe.get("ready") is not True
        or result["model_probe_age_seconds"] is None or result["model_probe_age_seconds"] > 65 * 60
    ):
        result["reasons"].append("model_probe_unavailable_or_stale")
    if cfg.enabled and result["adapter_ready"] is not True:
        result["reasons"].append("adapter_unavailable")
    if cfg.enabled and result["participant_registry_ready"] is not True:
        result["reasons"].append("participant_registry_unavailable")
    if cfg.enabled and result["cron_fresh"] is not True:
        result["reasons"].append("maintenance_cron_stale")
    if cfg.enabled and result["eligible_interests"] and (
        result["planning_attempt_age_seconds"] is None
        or result["planning_attempt_age_seconds"] > 65 * 60
    ):
        result["reasons"].append("eligible_interests_unplanned")
    if cfg.enabled and missing_eligible_digest:
        result["reasons"].append("digest_missing")
    if cfg.enabled and result["oldest_digest_age_seconds"] is not None and result["oldest_digest_age_seconds"] > 8 * 86400:
        result["reasons"].append("digest_stale")
    if cfg.enabled and not cfg.alarm_sink_configured:
        result["reasons"].append("alarm_sink_unconfigured")
    if cfg.enabled and (
        not isinstance(result["alarm_probe"], Mapping)
        or result["alarm_probe"].get("ready") is not True
        or result["alarm_probe"].get("delivery_ack") is not True
    ):
        result["reasons"].append("alarm_sink_probe_unavailable_or_stale")
    result["send_rate"] = (
        result["send_rate_sent"] / result["send_rate_total"] if result["send_rate_total"] else 0.0
    )
    if result["send_rate_total"] and result["send_rate"] > 0.40:
        result["reasons"].append("send_rate_above_40_percent")
    result["reasons"] = sorted(set(result["reasons"]))
    result["dead"] = bool(result["reasons"])
    return result


__all__ = ["health_snapshot", "probe_alarm_sink_readiness", "probe_model_readiness"]
