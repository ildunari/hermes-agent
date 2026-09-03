"""Compatibility seam for external authenticated-DM support."""

from hermes_cli.external_support import load_support_module


def _implementation():
    return load_support_module(
        "hermes_runtime_support.authenticated_dm",
        "support/hermes-runtime-support/src/hermes_runtime_support/authenticated_dm.py",
    )


def __getattr__(name):
    return getattr(_implementation(), name)
