"""Public module alias for support-owned conversation-extension contracts."""
import sys
from hermes_cli.external_support import load_support_module

sys.modules[__name__] = load_support_module(
    "hermes_runtime_support.conversation_extensions",
    "support/hermes-runtime-support/src/hermes_runtime_support/"
    "conversation_extensions.py",
)
