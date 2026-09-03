"""KEEP drain freshness metadata retained across the origin/main land."""

import asyncio
import json

import pytest


def test_active_work_write_stamps_dedicated_count_freshness(tmp_path, monkeypatch):
    from gateway import status

    path = tmp_path / "gateway_state.json"
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: path)

    status.write_runtime_status(gateway_state="running", active_agents=2)
    first = json.loads(path.read_text(encoding="utf-8"))
    assert first["active_agents"] == 2
    assert first["active_agents_updated_at"]

    status.write_runtime_status(gateway_state="running")
    second = json.loads(path.read_text(encoding="utf-8"))
    assert second["active_agents_updated_at"] == first["active_agents_updated_at"]


@pytest.mark.asyncio
async def test_loop_heartbeat_carries_a_fresh_active_work_snapshot(
    tmp_path, monkeypatch
):
    from gateway import shutdown_watchdog

    writes = []

    async def _to_thread(func, *args, **kwargs):
        if func is shutdown_watchdog.write_loop_heartbeat:
            writes.append(kwargs["extra"])
        return None

    monkeypatch.setattr(shutdown_watchdog.asyncio, "to_thread", _to_thread)
    task = asyncio.create_task(
        shutdown_watchdog.loop_heartbeat_forever(
            interval_s=60,
            home=tmp_path,
            extra_provider=lambda: {"active_agents": 3},
        )
    )
    try:
        for _ in range(50):
            if writes:
                break
            await asyncio.sleep(0)
        assert writes and writes[0]["active_agents"] == 3
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
