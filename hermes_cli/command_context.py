"""Compatibility seam for external CLI command-context support."""

from hermes_cli.external_support import load_support_module


def _implementation():
    return load_support_module(
        "hermes_runtime_support.cli_command_context",
        "support/hermes-runtime-support/src/hermes_runtime_support/cli_command_context.py",
    )


def __getattr__(name):
    return getattr(_implementation(), name)
