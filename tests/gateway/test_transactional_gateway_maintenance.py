import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from gateway import drain_control as dc


def test_transaction_survives_expiry_restart_and_requires_matching_release(tmp_path):
    owner = dc.GatewayMaintenance(tmp_path)
    owner.begin(owner.generation, "transaction")
    body = json.loads(owner.path.read_text())
    body.update(requested_at="2000-01-01T00:00:00+00:00", epoch="old-boot")
    owner.path.write_text(json.dumps(body))
    assert dc.drain_requested(home=tmp_path)
    replacement = dc.GatewayMaintenance(tmp_path)
    assert replacement.closed and replacement.generation != owner.generation
    with pytest.raises(ValueError):
        replacement.release(owner.generation, "transaction")
    with pytest.raises(ValueError):
        replacement.release(replacement.generation, "wrong")
    with pytest.raises(ValueError):
        dc.clear_drain_request(home=tmp_path)
    replacement.release(replacement.generation, "transaction")
    replacement.release(replacement.generation, "transaction")
    assert not replacement.closed
    assert dc.GatewayMaintenance(tmp_path).closed
    replacement.finalize(replacement.generation, "transaction")
    replacement.release(replacement.generation, "transaction")
    replacement.finalize(replacement.generation, "transaction")
    assert not dc.GatewayMaintenance(tmp_path).closed


def test_admitted_cron_dispatch_stays_counted_across_begin(tmp_path):
    owner = dc.GatewayMaintenance(tmp_path)
    with owner.admission() as admitted:
        assert admitted and owner.pending_admissions == 1
        owner.begin(owner.generation, "transaction")
        assert owner.pending_admissions == 1
        with owner.admission() as later:
            assert not later
    assert owner.pending_admissions == 0


@pytest.mark.asyncio
async def test_bootstrap_waits_before_startup_and_release_is_idempotent(tmp_path):
    original = dc.GatewayMaintenance(tmp_path)
    original.begin(original.generation, "transaction")
    replacement = dc.GatewayMaintenance(tmp_path)
    runner = SimpleNamespace(_shutdown_event=asyncio.Event())
    opened = []

    async def startup():
        await replacement.wait_until_open(runner)
        opened.append("adapters/resume/cron")

    task = asyncio.create_task(startup())
    await asyncio.sleep(0)
    assert not opened
    replacement.release(replacement.generation, "transaction")
    replacement.release(replacement.generation, "transaction")
    await asyncio.wait_for(task, 2)
    assert opened == ["adapters/resume/cron"]


def test_corrupt_transaction_fails_closed(tmp_path):
    dc.drain_request_path(tmp_path).write_text('{"protocol_version":1}')
    with pytest.raises(ValueError):
        dc.GatewayMaintenance(tmp_path)


@pytest.mark.asyncio
async def test_real_native_socket_ack_and_startup_hold(tmp_path, monkeypatch):
    from gateway.run import _start_gateway_start_control_socket
    from gateway.control_socket import query_gateway_control
    from tests.gateway.restart_test_helpers import make_restart_runner
    runner, _ = make_restart_runner()
    owner = dc.GatewayMaintenance(tmp_path)
    runner._maintenance_owner = owner
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    server = await _start_gateway_start_control_socket(runner)
    assert server is not None
    try:
        async def call(body):
            return await asyncio.to_thread(query_gateway_control, tmp_path, "owner_maintenance", body=body)
        status = await call({"action": "status"})
        assert status["owners"][0]["owner_generation"] == owner.generation
        held = await call({"action": "begin", "owner_generation": owner.generation, "request_token": "tx"})
        assert held["owners"][0]["admissions_closed"]
        # Run the real startup entry point; the first startup operation must
        # remain unreachable until the native socket releases this generation.
        entered = []
        monkeypatch.setattr(runner, "_install_conversation_extension_host", lambda: None)
        def first_startup_operation():
            entered.append(True)
            raise RuntimeError("startup reached")
        monkeypatch.setattr(runner, "_start_install_faulthandler", first_startup_operation)
        task = asyncio.create_task(runner.start())
        await asyncio.sleep(0)
        assert entered == []
        assert await call({"action": "release", "owner_generation": "stale", "request_token": "tx"}) is None
        assert owner.closed
        released = await call({"action": "release", "owner_generation": owner.generation, "request_token": "tx"})
        assert released["owners"][0]["bootstrap_held"]
        with pytest.raises(RuntimeError, match="startup reached"):
            await task
        assert entered == [True]
    finally:
        await server.stop()


def test_real_tick_pre_registration_gap_is_not_idle(tmp_path, monkeypatch):
    from cron import scheduler
    owner = dc.GatewayMaintenance(tmp_path)
    monkeypatch.setattr(dc, "_gateway_owner", owner)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
    monkeypatch.setattr(scheduler, "_maybe_reap_dead_owners", lambda: None)
    monkeypatch.setattr(scheduler, "_maybe_run_worktree_maintenance", lambda: None)
    entered, finish = threading.Event(), threading.Event()
    errors = []
    def due_jobs():
        entered.set()
        assert finish.wait(5)
        return []
    monkeypatch.setattr(scheduler, "get_due_jobs", due_jobs)
    def tick():
        try:
            scheduler.tick(verbose=False, sync=False)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=tick)
    thread.start()
    try:
        assert entered.wait(5)
        owner.begin(owner.generation, "tx")
        assert not scheduler.get_running_job_ids()
        assert owner.pending_admissions == 1
        assert scheduler.tick(verbose=False, sync=False) == 0
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert owner.pending_admissions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_held_native_turn_resumes_once_or_obeys_stop_generation(tmp_path, monkeypatch, stopped):
    from unittest.mock import AsyncMock
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
    runner, _ = make_restart_runner()
    owner = runner._maintenance_owner = dc.GatewayMaintenance(tmp_path)
    owner.begin(owner.generation, "tx")
    run = AsyncMock(return_value={"final_response": "done"})
    monkeypatch.setattr(runner, "_run_agent_in_profile", run)
    monkeypatch.setattr(runner, "_is_session_run_current", lambda *args: not stopped)
    task = asyncio.create_task(runner._run_agent("queued", "context", [], make_restart_source(), "session",
                                                 session_key="key", run_generation=1))
    await asyncio.sleep(0)
    assert owner.waiting_turns == 1 and run.await_count == 0
    owner.release(owner.generation, "tx")
    owner.release(owner.generation, "tx")
    result = await asyncio.wait_for(task, 2)
    assert run.await_count == (0 if stopped else 1)
    assert owner.waiting_turns == 0
    assert result == ({"final_response": "", "interrupted": True} if stopped else {"final_response": "done"})


def test_unknown_cron_provider_cannot_ack_maintenance(tmp_path):
    from tests.gateway.restart_test_helpers import make_restart_runner
    runner, _ = make_restart_runner()
    owner = dc.GatewayMaintenance(tmp_path)
    owner.provider_supported = False
    with pytest.raises(ValueError, match="cron provider"):
        owner.begin(owner.generation, "tx")
    with pytest.raises(ValueError, match="cron provider"):
        owner.status(runner)


@pytest.mark.asyncio
async def test_finalized_transaction_preserves_later_legacy_drain(tmp_path, monkeypatch):
    from tests.gateway.restart_test_helpers import make_restart_runner
    runner, _ = make_restart_runner()
    owner = runner._maintenance_owner = dc.GatewayMaintenance(tmp_path)
    owner.begin(owner.generation, "tx")
    owner.release(owner.generation, "tx")
    owner.finalize(owner.generation, "tx")
    dc.write_drain_request(home=tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async def end_iteration(_):
        runner._running = False
    monkeypatch.setattr(asyncio, "sleep", end_iteration)
    await runner._drain_control_watcher()
    assert runner._external_drain_active
