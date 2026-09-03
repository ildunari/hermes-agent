"""Compatibility seam for external gateway command-context support."""
import logging
import os
from pathlib import Path

from hermes_cli.external_support import load_support_module

logger = logging.getLogger(__name__)

def _implementation():
    return load_support_module(
        "hermes_runtime_support.gateway_command_context",
        "support/hermes-runtime-support/src/hermes_runtime_support/gateway_command_context.py",
    )

def __getattr__(name):
    return getattr(_implementation(), name)


async def dispatch_pending_plugin_command_followup(runner, event, session_key):
    """Fail open when optional command support is unavailable mid-update."""
    try:
        return await _implementation().dispatch_pending_plugin_command_followup(
            runner, event, session_key
        )
    except Exception:
        logger.warning(
            "Pending plugin command follow-up dispatch failed",
            exc_info=True,
        )
        return False


class GatewayCommandRuntimeMixin:
    """Thin host hooks for session-aware command preferences."""

    def _session_cwd_for_entry(self, entry):
        if entry is not None and getattr(entry, "cwd_override", None):
            return str(entry.cwd_override)
        try:
            from tools.terminal_scope import terminal_env

            raw = terminal_env("TERMINAL_CWD", str(Path.home()))
        except Exception:
            raw = os.getenv("TERMINAL_CWD") or str(Path.home())
        return os.path.abspath(os.path.expanduser(raw))

    def _session_entry_for_key(self, session_key):
        try:
            return self.session_store.lookup_by_session_key(session_key)
        except Exception:
            return None

    def _bind_task_cwd(self, task_id, cwd, session_key=None):
        if not task_id or not cwd:
            return
        try:
            from tools.terminal_tool import (
                record_session_cwd,
                register_task_env_overrides,
            )

            register_task_env_overrides(task_id, {"cwd": cwd})
            if session_key:
                record_session_cwd(session_key, cwd)
        except Exception:
            logger.debug(
                "Failed to bind cwd override for task %s", task_id, exc_info=True
            )

    def _session_personality_prompt(self, session_key: str) -> str:
        entry = self._session_entry_for_key(session_key)
        value = getattr(entry, "personality_override", None) if entry else None
        return str(value.get("prompt") or "").strip() if isinstance(value, dict) else ""

    async def _handle_detached_surface_restart_command(self, event, canonical: str) -> str:
        """Queue cross-surface restart work outside the receiving gateway."""
        from hermes_cli.restart_surfaces import enqueue_detached_restart

        scope = {
            "restart-gateways": "gateways",
            "restart-webui": "webui",
        }.get(canonical, "hermes")
        if scope == "webui" and event.source and event.source.platform:
            try:
                origin_platform = event.source.platform.value
            except Exception:
                origin_platform = ""
            if origin_platform == "api_server":
                return (
                    "Refusing to restart the WebUI from the WebUI surface "
                    "itself — send /restart-webui from Telegram (or another "
                    "chat platform), or run `hermes restart-webui` in a terminal."
                )
        args = event.get_command_args().split()
        dry_run = any(
            arg.lower() in {"--dry-run", "dry-run", "smoke", "test", "plan"}
            for arg in args
        )
        notify_origin = None
        if event.source and event.source.platform and event.source.chat_id:
            notify_origin = {
                "platform": event.source.platform.value,
                "chat_id": event.source.chat_id,
            }
            if event.source.thread_id:
                notify_origin["thread_id"] = event.source.thread_id
        return enqueue_detached_restart(
            scope,
            delay=1.0,
            dry_run=dry_run,
            notify_origin=notify_origin,
        )
