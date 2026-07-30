"""Shutdown watchdog + loop heartbeat coverage for #66892.

The drain path is asyncio-based; a frozen loop makes every asyncio timeout
structurally unable to fire. These tests pin the out-of-loop backstop
(thread watchdog) and the loop-liveness heartbeat file contract.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import patch

import pytest

from gateway.shutdown_watchdog import (
    DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
    arm_shutdown_watchdog,
    get_loop_heartbeat_path,
    get_shutdown_watchdog_dump_path,
    loop_heartbeat_forever,
    resolve_shutdown_watchdog_delay,
    write_loop_heartbeat,
)

def test_resolve_shutdown_watchdog_delay_adds_grace():
    assert resolve_shutdown_watchdog_delay(180) == 180 + DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(0) == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay("bad") == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(10, grace_s=5) == 15.0


def test_arm_shutdown_watchdog_fires_with_dump_and_exit(tmp_path):
    done = threading.Event()
    fired = threading.Event()
    dump = tmp_path / "logs" / "watchdog.log"
    snapshot_calls = []
    exit_codes = []

    def snapshot():
        snapshot_calls.append(1)
        return {"active_agents": 1, "draining": True}

    def fake_exit(code):
        exit_codes.append(code)
        fired.set()

    with patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit):
        arm_shutdown_watchdog(
            0.15,
            done_event=done,
            snapshot_fn=snapshot,
            dump_path=dump,
            exit_code=9,
        )
        assert fired.wait(timeout=5.0), "watchdog did not fire"

    assert exit_codes == [9]
    assert snapshot_calls == [1]
    assert dump.is_file()
    text = dump.read_text(encoding="utf-8")
    assert "shutdown_watchdog_fired" in text
    assert "faulthandler dump" in text
    assert get_shutdown_watchdog_dump_path(tmp_path).name == "gateway-shutdown-watchdog.log"


@pytest.mark.asyncio
async def test_loop_heartbeat_rewrites_until_cancelled(tmp_path):
    path = get_loop_heartbeat_path(tmp_path)
    task = asyncio.create_task(
        loop_heartbeat_forever(
            interval_s=0.05,
            start_time=12.0,
            home=tmp_path,
        )
    )
    try:
        # First write is immediate.
        for _ in range(50):
            if path.is_file():
                break
            await asyncio.sleep(0.02)
        assert path.is_file()
        first = path.read_text(encoding="utf-8")
        assert json.loads(first)["start_time"] == 12.0

        # Poll until a refresh lands (monotonic / updated_at change).
        second = first
        for _ in range(100):
            await asyncio.sleep(0.03)
            second = path.read_text(encoding="utf-8")
            if second != first:
                break
        assert second != first
        assert json.loads(second)["start_time"] == 12.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_loop_heartbeat_refreshes_runtime_status_identity_and_survives_errors(
    tmp_path,
):
    """Each heartbeat cycle re-stamps runtime-status identity (self-healing a
    foreign clobber of gateway_state.json), and a status-write failure must
    never kill the heartbeat loop."""
    calls = []

    def fake_write_runtime_status(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("status write failed")

    path = get_loop_heartbeat_path(tmp_path)
    with patch(
        "gateway.status.write_runtime_status",
        side_effect=fake_write_runtime_status,
    ):
        task = asyncio.create_task(
            loop_heartbeat_forever(interval_s=0.05, home=tmp_path)
        )
        try:
            # interval_s clamps to 1s; wait long enough for a post-failure cycle.
            for _ in range(300):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert len(calls) >= 2, "heartbeat loop died after a status-write failure"
    # The refresh is a no-payload owner-path write: identity only.
    assert all(kwargs == {} for kwargs in calls)
    assert path.is_file()


@pytest.mark.asyncio
async def test_loop_heartbeat_merges_extra_provider_every_tick(tmp_path):
    """extra_provider is called fresh each cycle and merged into the payload —
    the mechanism hermes_cli.restart_surfaces relies on to cross-check a
    stale gateway_state.json's active_agents (docs/local/
    UPDATE_INCIDENTS_20260723.md item 12)."""
    path = get_loop_heartbeat_path(tmp_path)
    counter = {"n": 0}

    def provider():
        counter["n"] += 1
        return {"active_agents": counter["n"]}

    task = asyncio.create_task(
        loop_heartbeat_forever(interval_s=0.05, home=tmp_path, extra_provider=provider)
    )
    try:
        for _ in range(50):
            if path.is_file():
                break
            await asyncio.sleep(0.02)
        assert path.is_file()
        first = json.loads(path.read_text(encoding="utf-8"))
        assert first["active_agents"] == 1

        second_value = first
        for _ in range(100):
            await asyncio.sleep(0.03)
            second_value = json.loads(path.read_text(encoding="utf-8"))
            if second_value["active_agents"] != first["active_agents"]:
                break
        assert second_value["active_agents"] > first["active_agents"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_loop_heartbeat_survives_broken_extra_provider(tmp_path):
    """A raising extra_provider must not kill the heartbeat loop — it is a
    liveness signal and must never itself become the single point of failure."""
    path = get_loop_heartbeat_path(tmp_path)

    def broken_provider():
        raise RuntimeError("active_work_count blew up")

    task = asyncio.create_task(
        loop_heartbeat_forever(interval_s=0.05, home=tmp_path, extra_provider=broken_provider)
    )
    try:
        for _ in range(50):
            if path.is_file():
                break
            await asyncio.sleep(0.02)
        assert path.is_file()
        first = path.read_text(encoding="utf-8")
        assert "active_agents" not in json.loads(first)

        # Confirm the loop keeps rewriting (didn't die) despite the provider
        # always raising.
        second = first
        for _ in range(100):
            await asyncio.sleep(0.03)
            second = path.read_text(encoding="utf-8")
            if second != first:
                break
        assert second != first
        assert "active_agents" not in json.loads(second)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_gateway_runner_exposes_shutdown_watchdog_state():
    """Attrs used by stop()/start() exist after normal construction hooks."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._shutdown_watchdog_done = threading.Event()
    runner._loop_heartbeat_task = None
    runner._gateway_started_at = time.time()
    assert not runner._shutdown_watchdog_done.is_set()
    runner._shutdown_watchdog_done.set()
    assert runner._shutdown_watchdog_done.is_set()
    assert runner._loop_heartbeat_task is None


def test_count_write_stamps_dedicated_freshness_field(tmp_path, monkeypatch):
    """write_runtime_status must stamp active_agents_updated_at when (and
    only when) it writes the count, and accept a callable re-snapshotted at
    write time (Codex fix-lane review P1-1/P1-2)."""
    import json as _json

    from gateway import status as status_mod

    path = tmp_path / "gateway_state.json"
    monkeypatch.setattr(status_mod, "_get_runtime_status_path", lambda: path)
    monkeypatch.setattr(status_mod, "_process_owns_runtime_status", lambda existing: True)

    status_mod.write_runtime_status(gateway_state="running", active_agents=lambda: 2)
    payload = _json.loads(path.read_text())
    assert payload["active_agents"] == 2
    first_stamp = payload["active_agents_updated_at"]
    assert first_stamp

    # Identity-style write without a count must NOT refresh the count stamp.
    status_mod.write_runtime_status(gateway_state="running")
    payload = _json.loads(path.read_text())
    assert payload["active_agents_updated_at"] == first_stamp

    # A failing counter callable skips the count write (unknown, never idle).
    def boom():
        raise RuntimeError("dictionary changed size during iteration")

    status_mod.write_runtime_status(active_agents=boom)
    payload = _json.loads(path.read_text())
    assert payload["active_agents"] == 2
@pytest.mark.asyncio
async def test_loop_heartbeat_rewrites_until_cancelled(tmp_path):
    path = get_loop_heartbeat_path(tmp_path)
    task = asyncio.create_task(
        loop_heartbeat_forever(
            interval_s=0.05,
            start_time=12.0,
            home=tmp_path,
        )
    )
    try:
        # First write is immediate.
        for _ in range(50):
            if path.is_file():
                break
            await asyncio.sleep(0.02)
        assert path.is_file()
        first = path.read_text(encoding="utf-8")
        assert json.loads(first)["start_time"] == 12.0

        # Poll until a refresh lands (monotonic / updated_at change).
        second = first
        for _ in range(100):
            await asyncio.sleep(0.03)
            second = path.read_text(encoding="utf-8")
            if second != first:
                break
        assert second != first
        assert json.loads(second)["start_time"] == 12.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_gateway_runner_exposes_shutdown_watchdog_state():
    """Attrs used by stop()/start() exist after normal construction hooks."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._shutdown_watchdog_done = threading.Event()
    runner._loop_heartbeat_task = None
    runner._gateway_started_at = time.time()
    assert not runner._shutdown_watchdog_done.is_set()
    runner._shutdown_watchdog_done.set()
    assert runner._shutdown_watchdog_done.is_set()
    assert runner._loop_heartbeat_task is None
