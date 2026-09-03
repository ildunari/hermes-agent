"""Compatibility seam for external gateway command-context support."""
from hermes_cli.external_support import load_support_module

def _implementation():
    return load_support_module(
        "hermes_runtime_support.gateway_command_context",
        "support/hermes-runtime-support/src/hermes_runtime_support/gateway_command_context.py",
    )

def __getattr__(name):
    return getattr(_implementation(), name)


class GatewayCommandRuntimeMixin:
    """Thin host hooks for session-aware command preferences."""

    def _session_cwd_for_entry(self, entry):
        return _implementation().session_cwd_for_entry(entry)

    def _session_personality_prompt(self, session_key: str) -> str:
        try:
            entry = self.session_store.lookup_by_session_key(session_key)
        except Exception:
            entry = None
        return _implementation().session_personality_prompt(entry)

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
