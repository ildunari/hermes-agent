"""Process-local supervision for durable browser-annotation turn queues.

The SQLite lineage repository remains authoritative.  This registry merely
coalesces wakeups, guarantees one local runner per lineage, and gives explicit
cancellation a prompt in-process interrupt path.  A process restart loses no
work: ``recover_and_start`` settles expired leases and scans durable queues.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from hermes_cli.browser_annotation_lineage import AnnotationLineageRepository
from hermes_cli.browser_annotation_worker import AnnotationWorkerResult


class LaneWorker(Protocol):
    def run_next(
        self,
        annotation_id: str,
        thread_generation: int,
        *,
        cancellation_event: object | None = None,
    ) -> AnnotationWorkerResult: ...


WorkerFactory = Callable[[], LaneWorker]
TerminalCallback = Callable[[AnnotationWorkerResult], None]


@dataclass
class _Lane:
    wake: threading.Event
    stop: threading.Event
    cancellation: "_TurnCancellation"
    thread: threading.Thread


class _TurnCancellation:
    """Reusable lane signal whose authority is scoped to exact turn ids."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._turn_ids: set[str] = set()

    def cancel(self, turn_id: str) -> None:
        with self._condition:
            self._turn_ids.add(str(turn_id))
            self._condition.notify_all()

    def is_turn_cancelled(self, turn_id: str) -> bool:
        with self._condition:
            return str(turn_id) in self._turn_ids

    def wait_for_turn(self, turn_id: str, timeout: float | None = None) -> bool:
        turn_id = str(turn_id)
        with self._condition:
            return self._condition.wait_for(
                lambda: turn_id in self._turn_ids,
                timeout=timeout,
            )


class AnnotationWorkerRegistry:
    """Bounded per-profile registry; never registers generic TUI sessions."""

    def __init__(
        self,
        repository: AnnotationLineageRepository,
        worker_factory: WorkerFactory,
        *,
        terminal_callback: TerminalCallback | None = None,
        idle_seconds: float = 60.0,
        recovery_seconds: float = 15.0,
    ) -> None:
        self.repository = repository
        self.worker_factory = worker_factory
        self.terminal_callback = terminal_callback
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        self.idle_seconds = float(idle_seconds)
        if recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be positive")
        self.recovery_seconds = float(recovery_seconds)
        self._lock = threading.RLock()
        self._lanes: dict[tuple[str, int], _Lane] = {}
        self._closed = False
        self._recovery_stop = threading.Event()
        self._recovery_thread: threading.Thread | None = None

    def recover_and_start(self) -> dict[str, int]:
        recovery = self.repository.recover_expired_turns()
        for annotation_id, generation in self.repository.queued_lineages():
            self.notify(annotation_id, generation)
        with self._lock:
            if self._closed:
                raise RuntimeError("annotation worker registry is closed")
            if self._recovery_thread is None or not self._recovery_thread.is_alive():
                self._recovery_stop.clear()
                self._recovery_thread = threading.Thread(
                    target=self._run_recovery,
                    name="annotation-lease-recovery",
                    daemon=True,
                )
                self._recovery_thread.start()
        return recovery

    def notify(self, annotation_id: str, thread_generation: int) -> None:
        key = (str(annotation_id), int(thread_generation))
        with self._lock:
            if self._closed:
                raise RuntimeError("annotation worker registry is closed")
            lane = self._lanes.get(key)
            if lane is not None and lane.thread.is_alive():
                lane.wake.set()
                return
            wake = threading.Event()
            stop = threading.Event()
            cancellation = _TurnCancellation()
            thread = threading.Thread(
                target=self._run_lane,
                args=(key, wake, stop, cancellation),
                name=f"annotation-lineage-{key[0]}-{key[1]}",
                daemon=True,
            )
            lane = _Lane(
                wake=wake, stop=stop, cancellation=cancellation, thread=thread
            )
            self._lanes[key] = lane
            wake.set()
            thread.start()

    def cancel(self, turn_id: str) -> str:
        # Durable cancellation is the authority and happens before the registry
        # wakeup.  The worker's lease/completion fence prevents a late commit.
        status = self.repository.cancel_turn(turn_id)
        turn = self.repository.turn(turn_id)
        if turn is not None:
            key = (str(turn["annotation_id"]), int(turn["thread_generation"]))
            with self._lock:
                lane = self._lanes.get(key)
                # Only a durable transition to cancelled authorizes an
                # in-process interrupt. A failed/completed race must not poison
                # the same turn id if the failed turn is later retried.
                if lane is not None and status == "cancelled":
                    lane.cancellation.cancel(turn_id)
                    lane.wake.set()
        return status

    def signal_cancelled(self, turn_ids: tuple[str, ...] | list[str]) -> None:
        """Interrupt turns already durably cancelled by a bundle transition."""

        for turn_id in turn_ids:
            turn = self.repository.turn(str(turn_id))
            if turn is None or turn.get("status") != "cancelled":
                continue
            key = (str(turn["annotation_id"]), int(turn["thread_generation"]))
            with self._lock:
                lane = self._lanes.get(key)
                if lane is not None:
                    lane.cancellation.cancel(str(turn_id))
                    lane.wake.set()

    def retry(self, turn_id: str) -> int:
        attempt = self.repository.retry_turn(turn_id)
        turn = self.repository.turn(turn_id)
        assert turn is not None
        self.notify(str(turn["annotation_id"]), int(turn["thread_generation"]))
        return attempt

    def close(self, *, timeout: float = 2.0) -> None:
        with self._lock:
            self._closed = True
            self._recovery_stop.set()
            recovery_thread = self._recovery_thread
            lanes = list(self._lanes.values())
            for lane in lanes:
                lane.stop.set()
                lane.wake.set()
        for lane in lanes:
            lane.thread.join(timeout=timeout)
        if recovery_thread is not None:
            recovery_thread.join(timeout=timeout)

    def _run_recovery(self) -> None:
        while not self._recovery_stop.wait(self.recovery_seconds):
            try:
                self.repository.recover_expired_turns(now=time.time())
                for annotation_id, generation in self.repository.queued_lineages():
                    self.notify(annotation_id, generation)
            except Exception:
                # Transient SQLite/profile-home failures retry on the next
                # bounded sweep without logging message or provider content.
                continue

    def active_lineages(self) -> tuple[tuple[str, int], ...]:
        with self._lock:
            return tuple(
                key for key, lane in self._lanes.items() if lane.thread.is_alive()
            )

    def _run_lane(
        self,
        key: tuple[str, int],
        wake: threading.Event,
        stop: threading.Event,
        cancellation: _TurnCancellation,
    ) -> None:
        annotation_id, generation = key
        try:
            worker = self.worker_factory()
            while not stop.is_set():
                notified = wake.wait(timeout=self.idle_seconds)
                if not notified:
                    # Remove under the same lock used by ``notify`` before
                    # exiting. Re-check the event under that lock: a notifier
                    # can set it after wait() times out but before we acquire
                    # the lock, while still observing this thread as alive.
                    with self._lock:
                        if not wake.is_set() and not stop.is_set():
                            current = self._lanes.get(key)
                            if (
                                current is not None
                                and current.thread is threading.current_thread()
                            ):
                                self._lanes.pop(key, None)
                            return
                    # A notify/stop raced with the timeout. Fall through and
                    # consume that signal instead of stranding durable work.
                wake.clear()
                if stop.is_set():
                    return
                while not stop.is_set():
                    result = worker.run_next(
                        annotation_id,
                        generation,
                        cancellation_event=cancellation,
                    )
                    if not result.claimed:
                        break
                    if self.terminal_callback is not None:
                        self.terminal_callback(result)
        finally:
            with self._lock:
                current = self._lanes.get(key)
                if current is not None and current.thread is threading.current_thread():
                    self._lanes.pop(key, None)