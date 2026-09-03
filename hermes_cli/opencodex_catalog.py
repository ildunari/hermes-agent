"""Compatibility seam for external OpenCodex catalog support."""
from hermes_cli.external_support import load_support_module

def _implementation():
    return load_support_module(
        "hermes_runtime_support.opencodex_catalog",
        "support/hermes-runtime-support/src/hermes_runtime_support/opencodex_catalog.py",
    )

def __getattr__(name):
    return getattr(_implementation(), name)
