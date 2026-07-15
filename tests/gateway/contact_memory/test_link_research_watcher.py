from __future__ import annotations

import asyncio
import threading

import pytest


@pytest.mark.asyncio
async def test_link_research_watcher_is_retained_and_joined_before_shutdown_returns(
    tmp_path, monkeypatch,
) -> None:
    from gateway.run import GatewayRunner
    import gateway.run as run_module
    import gateway.contact_memory.link_research_worker as worker_module
    import gateway.contact_memory.live_ingress as ingress_module
    import gateway.contact_memory.private_link_queue as queue_module
    import hermes_cli.profiles as profiles_module

    entered = threading.Event()
    release = threading.Event()
    writes: list[str] = []

    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "test")
    monkeypatch.setattr(profiles_module, "get_profile_dir", lambda profile: tmp_path / profile)
    monkeypatch.setattr(profiles_module, "list_profiles", lambda: [])
    monkeypatch.setattr(
        run_module, "_load_gateway_config_for_profile",
        lambda profile: {"agent": {"contact_memory": {"link_research": {
            "enabled": True, "interval_seconds": 300,
        }}}},
    )
    monkeypatch.setattr(ingress_module, "load_or_create_communication_key", lambda root: b"x" * 32)
    monkeypatch.setattr(
        worker_module, "build_configured_link_research_provider", lambda secret: object()
    )
    monkeypatch.setattr(queue_module, "discover_contacts_with_queues", lambda root: ("contact",))

    def process_one_link_job(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        writes.append("committed")
        return {"processed": True}

    monkeypatch.setattr(worker_module, "process_one_link_job", process_one_link_job)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._background_tasks = set()
    runner._contact_link_research_stop = asyncio.Event()
    task = asyncio.create_task(runner._contact_link_research_watcher())
    runner._contact_link_research_task = task
    runner._background_tasks.add(task)
    task.add_done_callback(runner._background_tasks.discard)

    assert await asyncio.to_thread(entered.wait, 5)
    assert task in runner._background_tasks
    runner._running = False
    stopping = asyncio.create_task(runner._stop_contact_link_research_watcher())
    await asyncio.sleep(0)
    assert not stopping.done()
    assert writes == []

    release.set()
    await asyncio.wait_for(stopping, timeout=5)
    assert task.done()
    assert writes == ["committed"]
    await asyncio.sleep(0.02)
    assert writes == ["committed"]
