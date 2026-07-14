"""Process-local contact-memory service construction."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from .broker import ContactMemoryBroker
from .embeddings import backend_from_config
from .extractor import PostTurnExtractionRuntime, extractor_from_config
from .rerankers import reranker_from_config


_brokers: dict[str, ContactMemoryBroker] = {}
_brokers_lock = threading.Lock()
_extractors: dict[str, PostTurnExtractionRuntime] = {}
_extractors_lock = threading.Lock()


def get_broker(root: str | Path, config: dict[str, Any]) -> ContactMemoryBroker:
    """Return the shared broker for one profile root and backend configuration."""

    root = Path(root).resolve()
    backend_key = json.dumps(
        {"embedding": config.get("embedding"), "reranker": config.get("reranker"), "retrieval": config.get("retrieval")},
        sort_keys=True,
        default=str,
    )
    key = f"{root}\0{backend_key}"
    with _brokers_lock:
        broker = _brokers.get(key)
        if broker is None:
            retrieval = config.get("retrieval")
            retrieval = retrieval if isinstance(retrieval, dict) else {}
            broker = ContactMemoryBroker(
                root,
                embedding_backend=backend_from_config(config),
                reranker=reranker_from_config(config),
                minimum_reranker_score=float(retrieval.get("minimum_reranker_score", -6.5)),
            )
            _brokers[key] = broker
        return broker


def get_extraction_runtime(
    root: str | Path, config: dict[str, Any]
) -> PostTurnExtractionRuntime | None:
    """Return one bounded extractor runtime per profile/config/event loop."""
    backend = extractor_from_config(config)
    if backend is None:
        return None
    root = Path(root).resolve()
    raw = config.get("extractor") if isinstance(config.get("extractor"), dict) else {}
    runtime_raw = config.get("extraction_runtime")
    runtime_raw = runtime_raw if isinstance(runtime_raw, dict) else {}
    loop = asyncio.get_running_loop()
    runtime_key = {"extractor": raw, "extraction_runtime": runtime_raw}
    key = f"{root}\0{id(loop)}\0{json.dumps(runtime_key, sort_keys=True, default=str)}"
    with _extractors_lock:
        runtime = _extractors.get(key)
        if runtime is None:
            runtime = PostTurnExtractionRuntime(
                backend,
                max_queue=int(runtime_raw.get("max_queue") or 32),
                workers=int(runtime_raw.get("workers") or 1),
                max_retries=int(runtime_raw.get("max_retries") or 1),
            )
            _extractors[key] = runtime
        else:
            # We constructed a redundant backend solely to resolve configuration.
            # It has not started a process, but close it for lifecycle symmetry.
            close = getattr(backend, "close", None)
            if callable(close):
                close()
        return runtime


def extraction_health(root: str | Path) -> dict[str, Any]:
    prefix = f"{Path(root).expanduser().resolve()}\0"
    with _extractors_lock:
        runtimes = [runtime for key, runtime in _extractors.items() if key.startswith(prefix)]
    snapshots = [{
        "configured_workers": runtime.worker_count,
        "live_workers": sum(not task.done() for task in runtime._tasks),
        "dead_workers": sum(task.done() and not task.cancelled() for task in runtime._tasks),
        "queue_depth": runtime.queue.qsize(),
        "queue_capacity": runtime.queue.maxsize,
        "queue_full": runtime.queue.full(),
        "failures": runtime.failures,
        "dropped": runtime.dropped,
    } for runtime in runtimes]
    return {
        "runtime_count": len(snapshots),
        "configured_workers": sum(int(item["configured_workers"]) for item in snapshots),
        "live_workers": sum(int(item["live_workers"]) for item in snapshots),
        "dead_workers": sum(int(item["dead_workers"]) for item in snapshots),
        "queue_depth": sum(int(item["queue_depth"]) for item in snapshots),
        "queue_capacity": sum(int(item["queue_capacity"]) for item in snapshots),
        "queue_full": any(bool(item["queue_full"]) for item in snapshots),
        "failures": sum(int(item["failures"]) for item in snapshots),
        "dropped": sum(int(item["dropped"]) for item in snapshots),
    }


async def close_extraction_runtimes(*, timeout: float = 10.0) -> None:
    """Drain and stop all local extractor workers without hanging shutdown."""
    with _extractors_lock:
        runtimes = list(_extractors.values())
        _extractors.clear()
    if not runtimes:
        return
    close_tasks = [asyncio.create_task(runtime.close()) for runtime in runtimes]
    done, pending = await asyncio.wait(close_tasks, timeout=max(0.1, float(timeout)))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(
            *(runtime.force_close() for runtime in runtimes), return_exceptions=True
        )
    for task in done:
        try:
            task.result()
        except Exception:
            pass


async def close_brokers() -> None:
    """Close and evict all cached retrieval subprocesses off the event loop."""
    with _brokers_lock:
        brokers = list(_brokers.values())
        _brokers.clear()
    await asyncio.gather(
        *(asyncio.to_thread(broker.close) for broker in brokers),
        return_exceptions=True,
    )
