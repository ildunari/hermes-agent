"""Reload-stable process state shared by browser tool module incarnations."""

from __future__ import annotations

from typing import Any


ACTIVE_SESSIONS: dict[str, dict[str, Any]] = {}
