import asyncio
import threading

import pytest

from gateway import drain_control as dc
from gateway import run as gateway_run
from gateway.control_socket import query_gateway_control
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
@pytest.mark.parametrize("held_replacement,cancel_startup", [(False, False), (True, False), (False, True)])
async def test_native_control_covers_discovery_before_runner_start(tmp_path, monkeypatch, held_replacement, cancel_startup):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if held_replacement:
        prior = dc.GatewayMaintenance(tmp_path)
        prior.begin(prior.generation, "tx")
    runner, _ = make_restart_runner()
    owner = runner._maintenance_owner = dc.GatewayMaintenance(tmp_path)
    monkeypatch.setattr(dc, "_gateway_owner", owner)
    monkeypatch.setattr(gateway_run, "GatewayRunner", lambda config: runner)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(gateway_run, "_start_gateway_claim_pid_file", lambda: True)
    monkeypatch.setattr(gateway_run, "_start_gateway_configure_logging", lambda *args: None)
    monkeypatch.setattr(gateway_run, "_enable_multiplex_log_routing", lambda *args: None)
    monkeypatch.setattr(gateway_run, "_run_planned_stop_watcher", lambda *args: None)
    monkeypatch.setattr(gateway_run, "_best_effort", lambda *args: None)
    monkeypatch.setattr(gateway_run, "_ensure_windows_gateway_venv_imports", lambda: None)
    monkeypatch.setattr(runner, "_install_conversation_extension_host", lambda: None)
    def stop_after_discovery():
        raise RuntimeError("startup after discovery")
    monkeypatch.setattr(runner, "_start_install_faulthandler", stop_after_discovery)

    finish = threading.Event()
    entered = threading.Event()
    attempted = asyncio.Event()
    def discover():
        entered.set()
        assert finish.wait(5)
    monkeypatch.setattr("tools.mcp_tool_discovery.discover_mcp_tools", discover)
    native_discover = gateway_run._discover_gateway_mcp_tools
    async def observe_discovery(config):
        attempted.set()
        await native_discover(config)
    monkeypatch.setattr(gateway_run, "_discover_gateway_mcp_tools", observe_discovery)

    ready = asyncio.Event()
    socket_return = asyncio.Event()
    servers = []
    native_socket = gateway_run._start_gateway_start_control_socket
    async def capture_socket(runner):
        server = await native_socket(runner)
        assert server is not None
        servers.append(server)
        ready.set()
        await socket_return.wait()  # The listener is reachable before start() returns.
        return server
    monkeypatch.setattr(gateway_run, "_start_gateway_start_control_socket", capture_socket)
    async def control(action):
        return await asyncio.to_thread(query_gateway_control, tmp_path, "owner_maintenance",
            body={"action": action, "owner_generation": owner.generation, "request_token": "tx"})

    task = asyncio.create_task(gateway_run.start_gateway(config=runner.config, verbosity=None))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        initial = await control("status")
        assert initial["owners"][0]["admissions_closed"] == held_replacement
        if not held_replacement:
            assert initial["owners"][0]["active_turns"] > 0, "socket exposure reported idle"
        socket_return.set()
        if held_replacement:
            status = await control("status")
            assert status["owners"][0]["admissions_closed"]
            assert not attempted.is_set(), "held replacement entered discovery before release"
            await control("release")
        await asyncio.wait_for(attempted.wait(), 2)
        assert await asyncio.to_thread(entered.wait, 2)
        status = await control("status")
        assert status["owners"][0]["active_turns"] > 0, "discovery reported idle"
        if cancel_startup:
            task.cancel()
            cancelled = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(cancelled[0], asyncio.CancelledError)
            status = await control("status")
            assert status["owners"][0]["active_turns"] > 0, "cancelled discovery executor reported idle"
        elif not held_replacement:
            begun = await control("begin")
            assert begun["owners"][0]["admissions_closed"]
            assert begun["owners"][0]["active_turns"] > 0
            finish.set()
            completed = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            assert isinstance(completed[0], RuntimeError)
            assert str(completed[0]) == "startup after discovery"
            assert owner.closed and owner.pending_admissions == 0
    finally:
        socket_return.set()
        finish.set()
        if owner.closed:
            owner.release(owner.generation, "tx")
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        async def wait_for_reservation():
            while owner.pending_admissions:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait_for_reservation(), 3)
        for server in servers:
            await server.stop()
    if cancel_startup:
        assert isinstance(result[0], asyncio.CancelledError)
    else:
        assert isinstance(result[0], RuntimeError) and str(result[0]) == "startup after discovery"
    assert owner.pending_admissions == 0
