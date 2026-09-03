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
