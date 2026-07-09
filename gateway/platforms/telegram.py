"""Backward-compatible import shim for the Telegram platform plugin.

Telegram now lives under ``plugins.platforms.telegram.adapter``.  Local tests and
older integrations still import ``gateway.platforms.telegram`` directly, so keep
this thin re-export while the plugin path remains canonical.
"""

from plugins.platforms.telegram.adapter import *  # noqa: F401,F403
