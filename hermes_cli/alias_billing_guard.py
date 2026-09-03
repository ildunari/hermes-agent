"""Compatibility seam for external provider billing policy support."""
from hermes_cli.external_support import load_support_module

def _implementation():
    return load_support_module(
        "hermes_runtime_support.alias_billing_guard",
        "support/hermes-runtime-support/src/hermes_runtime_support/alias_billing_guard.py",
    )

def __getattr__(name):
    return getattr(_implementation(), name)
