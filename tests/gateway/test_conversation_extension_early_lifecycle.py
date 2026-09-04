import asyncio
import threading

import pytest

from gateway.conversation_extension_host import (
    _mark_full_host_ready,
    install_early_lifecycle_scheduling,
)
from gateway.conversation_extensions import (
    AuxiliaryModelRequest,
    GatewayBackgroundTask,
    GatewayHostOperations,
    GatewayRuntimeFacade,
    gateway_host_operations,
    install_gateway_host_operations,
    lifecycle_task_registry,
    reset_gateway_host_operations,
)


@pytest.mark.asyncio
async def test_early_lifecycle_marshals_worker_registration_to_gateway_loop():
    reset_gateway_host_operations()
    lifecycle_task_registry.reset_for_tests()
    try:
        owner_thread = threading.get_ident()
        started = asyncio.Event()
        factory_threads = []

        async def watcher():
            factory_threads.append(threading.get_ident())
            started.set()

        install_early_lifecycle_scheduling()
        spawn = gateway_host_operations().spawn_task
        assert spawn is not None
        task = GatewayBackgroundTask(
            identity=("poke", "/tmp/poke", "proactive-watcher", 1),
            factory=watcher,
        )

        await asyncio.to_thread(spawn, task)
        await asyncio.sleep(0)
        assert not started.is_set()
        from gateway.run import GatewayRunner

        object.__new__(GatewayRunner)._install_conversation_extension_host()
        await asyncio.sleep(0)
        assert not started.is_set()
        _mark_full_host_ready()
        await asyncio.wait_for(started.wait(), timeout=1)

        assert factory_threads == [owner_thread]
        assert lifecycle_task_registry.active_generations("poke", "/tmp/poke") == (1,)
    finally:
        lifecycle_task_registry.cancel("poke", "/tmp/poke", 1)
        lifecycle_task_registry.reset_for_tests()
        reset_gateway_host_operations()


@pytest.mark.asyncio
async def test_early_facade_upgrades_to_full_host_operations():
    reset_gateway_host_operations()
    lifecycle_task_registry.reset_for_tests()
    try:
        install_early_lifecycle_scheduling()
        facade = GatewayRuntimeFacade(
            extension_id="poke",
            profile_name="poke",
            profile_home="/tmp/poke",
            generation=1,
            capabilities=frozenset({"lifecycle", "auxiliary_model"}),
            host=gateway_host_operations(),
        )
        request = AuxiliaryModelRequest(
            task="probe",
            provider="openai-codex",
            model="gpt-5.6-sol",
            messages=({"role": "user", "content": "OK"},),
            reasoning_effort="low",
            max_tokens=4,
        )

        install_gateway_host_operations(
            GatewayHostOperations(call_auxiliary_model=lambda received: "OK")
        )

        assert facade.call_auxiliary_model(request) == "OK"
    finally:
        lifecycle_task_registry.reset_for_tests()
        reset_gateway_host_operations()
