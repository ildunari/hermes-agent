"""Safe, additive runtime routing lifecycle events.

This module deliberately exposes only provider/model identity.  Runtime
credentials, endpoints, pool labels, and exception text never cross the event
callback boundary.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_REASON_MAP = {
    "auth": "authentication",
    "auth_permanent": "authentication",
    "billing": "billing",
    "rate_limit": "rate_limit",
    "upstream_rate_limit": "rate_limit",
    "overloaded": "provider_unavailable",
    "server_error": "upstream_error",
    "timeout": "timeout",
    "context_overflow": "context_limit",
    "payload_too_large": "context_limit",
    "model_not_found": "provider_unavailable",
    "format_error": "non_retryable_error",
    "content_policy_blocked": "non_retryable_error",
    "provider_policy_blocked": "non_retryable_error",
}


def _identity(model: Any, provider: Any) -> dict[str, str]:
    return {"model": str(model or ""), "provider": str(provider or "")}


def normalize_fallback_reason(reason: Any) -> str:
    value = getattr(reason, "value", reason)
    return _REASON_MAP.get(str(value or "").strip().lower(), "unknown")


def build_runtime_route(agent: Any, state: str, *, reason: Any = None) -> dict[str, Any]:
    primary = getattr(agent, "_primary_runtime", None)
    if not isinstance(primary, dict):
        primary = {}
    selected = getattr(agent, "_selected_runtime_identity", None)
    if not isinstance(selected, dict):
        selected = primary
    selected_model = selected.get("model", getattr(agent, "model", ""))
    selected_provider = selected.get("provider", getattr(agent, "provider", ""))
    active = bool(getattr(agent, "_fallback_activated", False))
    chain_index = max(0, int(getattr(agent, "_fallback_index", 0) or 0) - (1 if active else 0))
    payload = {
        "schema_version": 1,
        "state": state,
        "selected": _identity(selected_model, selected_provider),
        "runtime": _identity(getattr(agent, "model", ""), getattr(agent, "provider", "")),
        "fallback": {
            "active": active,
            "reason": normalize_fallback_reason(reason) if active else "unknown",
            "chain_index": chain_index,
        },
    }
    # Preserve the activation reason through finished/session.info snapshots.
    if active and reason is None:
        payload["fallback"]["reason"] = str(getattr(agent, "_runtime_route_reason", "unknown") or "unknown")
    return payload


def emit_runtime_route(agent: Any, state: str, *, reason: Any = None) -> dict[str, Any]:
    payload = build_runtime_route(agent, state, reason=reason)
    if payload["fallback"]["active"] and reason is not None:
        agent._runtime_route_reason = payload["fallback"]["reason"]
    elif not payload["fallback"]["active"]:
        agent._runtime_route_reason = "unknown"
    agent._runtime_routing = payload
    callback = getattr(agent, "event_callback", None)
    if callable(callback):
        try:
            callback("runtime:route", payload)
        except Exception:
            logger.debug("event_callback error on runtime:route", exc_info=True)
    return payload
