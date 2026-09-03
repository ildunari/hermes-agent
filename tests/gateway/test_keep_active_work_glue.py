"""Regression coverage for KEEP active-work persistence after the main land."""

from types import SimpleNamespace
from unittest.mock import patch

from gateway.active_work import persist_active_agents


def test_persist_active_agents_writes_the_current_numeric_count():
    runner = SimpleNamespace(_active_work_count=lambda: 2)

    with patch("gateway.status.write_runtime_status") as write_status:
        persist_active_agents(runner)

    write_status.assert_called_once_with(active_agents=2)
