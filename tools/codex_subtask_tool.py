"""Hermes tool surface for Codex-native subtasks."""
from __future__ import annotations

import json
import os
import shutil
from typing import Any

from agent.codex_subtask import client
from tools.registry import registry

_TIMEOUT = (
    "Sync create calls default to 600s and are capped at 600s; async create calls "
    "have no soft default and a 86400s hard wall. Effective timeout is echoed back."
)
_ACTIONS = {"create", "submit", "status", "await", "cancel", "send", "list", "logs"}
_MODEL_ACTIONS = {"create", "status", "await", "cancel", "send", "list", "logs"}


def _check() -> bool:
    return bool(shutil.which("codex")) or client.DEFAULT_SOCKET.exists()


def _session_id(kw: dict[str, Any]) -> str:
    parent = kw.get("parent_agent")
    return getattr(parent, "session_id", None) or os.environ.get("HERMES_SESSION_ID") or "default"


def _json_error(message: str) -> str:
    return json.dumps({"status": "error", "error": message}, ensure_ascii=False)


def _require_string(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _submit(args: dict[str, Any], **kw) -> str:
    payload = dict(args)
    payload.pop("action", None)
    if not _require_string(payload, "prompt"):
        return _json_error('codex_subtask action="create" requires non-empty prompt')
    payload.setdefault("mode", "sync")
    payload.setdefault("profile", "gpt")
    payload.setdefault("cwd", os.getcwd())
    payload["hermes_session_id"] = _session_id(kw)
    sock_timeout = (
        max(int(payload.get("timeout_seconds") or 600) + 20, 30)
        if payload.get("mode", "sync") == "sync"
        else 30
    )
    return json.dumps(
        client.request("submit", **payload, _socket_timeout=sock_timeout),
        ensure_ascii=False,
    )


def _request(action: str, args: dict[str, Any]) -> str:
    payload = dict(args)
    payload.pop("action", None)
    if action in {"status", "await", "cancel", "send", "logs"}:
        if not _require_string(payload, "job_id"):
            return _json_error(f'codex_subtask action="{action}" requires job_id')
    if action == "send" and not _require_string(payload, "message"):
        return _json_error('codex_subtask action="send" requires non-empty message')
    if action in {"status", "cancel"}:
        allowed = {"job_id"}
    elif action == "await":
        allowed = {"job_id", "timeout_seconds"}
    elif action == "send":
        allowed = {"job_id", "message", "timeout_seconds"}
    elif action == "list":
        limit = payload.get("limit")
        if isinstance(limit, int) and limit > 200:
            return _json_error('codex_subtask action="list" supports limit <= 200')
        allowed = {"filter", "status", "limit"}
    elif action == "logs":
        allowed = {"job_id", "since", "limit"}
    else:  # pragma: no cover - guarded by caller
        allowed = set(payload)
    payload = {k: v for k, v in payload.items() if k in allowed}
    return json.dumps(client.request(action, **payload), ensure_ascii=False)


def _call(args: dict[str, Any], **kw) -> str:
    raw_action = args.get("action") or "create"
    action = str(raw_action).strip().lower()
    if action not in _ACTIONS:
        return _json_error(
            "unknown codex_subtask action: "
            f"{raw_action!r}; expected one of {', '.join(sorted(_ACTIONS))}"
        )
    if action in {"create", "submit"}:
        return _submit(args, **kw)
    return _request(action, args)


def _simple(action: str, args: dict[str, Any], **kw) -> str:
    """Backward-compatible handlers for old direct lifecycle tool names."""
    return _request(action, args)


CODEX_SUBTASK_SCHEMA = {
    "name": "codex_subtask",
    "description": (
        "Durable Codex app-server job control. Use action=\"create\" to delegate a "
        "discrete task to a full Codex subagent with Codex shell/apply_patch/"
        "update_plan/view_image, plugins, skills, and Hermes MCP callback tools; use "
        "action=status/await/cancel/send/list/logs to manage async jobs. "
        "delegate_task remains the generic synchronous Hermes delegation tool; "
        "codex_subtask is for Codex-native durable jobs. "
        + _TIMEOUT
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_MODEL_ACTIONS),
                "default": "create",
                "description": "create starts a Codex job; lifecycle actions manage existing jobs.",
            },
            "prompt": {
                "type": "string",
                "description": "Required for action=create. Include expected output and constraints.",
            },
            "mode": {"type": "string", "enum": ["sync", "async"], "default": "sync"},
            "cwd": {"type": "string"},
            "model": {"type": "string"},
            "profile": {
                "type": "string",
                "description": "Codex v2 profile name (loads ~/.codex/<name>.config.toml). Defaults to gpt for Hermes GPT sessions.",
            },
            "reasoning_effort": {"type": "string", "enum": ["minimal", "low", "medium", "high"]},
            "timeout_seconds": {"type": "integer", "minimum": 1, "description": _TIMEOUT},
            "sandbox_mode": {"type": "string"},
            "allow_plugins": {"type": "array", "items": {"type": "string"}},
            "deny_plugins": {"type": "array", "items": {"type": "string"}},
            "skills": {"type": "array", "items": {"type": "string"}},
            "context_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to inline into the prompt, max 20, 50k chars each.",
            },
            "job_id": {"type": "string", "description": "Required for status/await/cancel/send/logs."},
            "message": {"type": "string", "description": "Required for action=send."},
            "filter": {"type": "string", "description": "Optional status filter for action=list."},
            "status": {"type": "string", "description": "Optional status filter for action=list."},
            "since": {"type": "integer", "minimum": 0, "description": "Transcript cursor for action=logs."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": "For action=list max 200; for action=logs max 1000.",
            },
        },
        "required": [],
    },
}

registry.register(
    name="codex_subtask",
    toolset="codex",
    schema=CODEX_SUBTASK_SCHEMA,
    handler=_call,
    check_fn=_check,
    emoji="🤖",
)

# Backward-compatible dispatch aliases. These remain registered so existing
# saved trajectories/tests/direct callers still work, but default GPT/Telegram
# toolsets no longer expose their schemas.
registry.register(
    name="codex_subtask_status",
    toolset="codex_legacy",
    schema={
        "name": "codex_subtask_status",
        "description": "Get status for a Codex subtask job.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    },
    handler=lambda args, **kw: _simple("status", args, **kw),
    check_fn=_check,
    emoji="🤖",
)
registry.register(
    name="codex_subtask_await",
    toolset="codex_legacy",
    schema={
        "name": "codex_subtask_await",
        "description": "Block until a Codex subtask reaches a terminal state or timeout.",
        "parameters": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1}},
            "required": ["job_id"],
        },
    },
    handler=lambda args, **kw: _simple("await", args, **kw),
    check_fn=_check,
    emoji="🤖",
)
registry.register(
    name="codex_subtask_cancel",
    toolset="codex_legacy",
    schema={
        "name": "codex_subtask_cancel",
        "description": "Cancel a running Codex subtask job.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    },
    handler=lambda args, **kw: _simple("cancel", args, **kw),
    check_fn=_check,
    emoji="🤖",
)
registry.register(
    name="codex_subtask_send",
    toolset="codex_legacy",
    schema={
        "name": "codex_subtask_send",
        "description": "Send a follow-up message into a running Codex subtask thread.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "message": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1},
            },
            "required": ["job_id", "message"],
        },
    },
    handler=lambda args, **kw: _simple("send", args, **kw),
    check_fn=_check,
    emoji="🤖",
)
registry.register(
    name="codex_subtask_list",
    toolset="codex_legacy",
    schema={
        "name": "codex_subtask_list",
        "description": "List active and recent Codex subtask jobs.",
        "parameters": {
            "type": "object",
            "properties": {
                "filter": {"type": "string"},
                "status": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": [],
        },
    },
    handler=lambda args, **kw: _simple("list", args, **kw),
    check_fn=_check,
    emoji="🤖",
)
registry.register(
    name="codex_subtask_logs",
    toolset="codex_legacy",
    schema={
        "name": "codex_subtask_logs",
        "description": "Fetch projected transcript/log events for a Codex subtask.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "since": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["job_id"],
        },
    },
    handler=lambda args, **kw: _simple("logs", args, **kw),
    check_fn=_check,
    emoji="🤖",
)
