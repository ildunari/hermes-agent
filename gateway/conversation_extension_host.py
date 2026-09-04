"""Compatibility seam for the support-owned conversation-extension host."""

from hermes_cli.external_support import load_support_module


def _implementation():
    return load_support_module(
        "hermes_runtime_support.conversation_extension_host",
        "support/hermes-runtime-support/src/hermes_runtime_support/"
        "conversation_extension_host.py",
    )


_impl = _implementation()

ConversationExtensionHostMixin = _impl.ConversationExtensionHostMixin
install_early_lifecycle_scheduling = _impl.install_early_lifecycle_scheduling
_mark_full_host_ready = _impl._mark_full_host_ready

__all__ = [
    "ConversationExtensionHostMixin",
    "install_early_lifecycle_scheduling",
    "_mark_full_host_ready",
]
