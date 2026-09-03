"""Compatibility seam for external conversation-extension runtime support."""

from hermes_cli.external_support import load_support_module


def _implementation():
    return load_support_module(
        "hermes_runtime_support.conversation_extension_runtime",
        "support/hermes-runtime-support/src/hermes_runtime_support/conversation_extension_runtime.py",
    )


def __getattr__(name):
    return getattr(_implementation(), name)
