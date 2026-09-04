import asyncio
import threading

import pytest

from gateway.conversation_extension_host import install_early_lifecycle_scheduling
from gateway.conversation_extensions import (
    GatewayBackgroundTask,
    gateway_host_operations,
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
        await asyncio.wait_for(started.wait(), timeout=1)

        assert factory_threads == [owner_thread]
        assert lifecycle_task_registry.active_generations("poke", "/tmp/poke") == (1,)
    finally:
        lifecycle_task_registry.cancel("poke", "/tmp/poke", 1)
        lifecycle_task_registry.reset_for_tests()
        reset_gateway_host_operations()
