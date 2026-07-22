from __future__ import annotations

import hashlib
import threading
import time
from typing import cast

import pytest

from hermes_cli.browser_annotation_lineage import AnnotationLineageRepository
from hermes_cli.browser_annotation_worker import AnnotationWorkerResult
from hermes_cli.browser_annotation_worker_registry import AnnotationWorkerRegistry, _Lane
from hermes_state import SessionDB


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _repo(tmp_path):
    path = tmp_path / "state.db"
    SessionDB(db_path=path).close()
    repo = AnnotationLineageRepository(profile_id="coding", state_db_path=path)
    repo.create_lineage(
        annotation_id="ann",
        annotation_lineage_root_id="root",
        model="model",
        model_config={"tools": []},
        system_prompt="system",
    )
    return repo


def _submit(repo, name):
    return repo.submit_human_message(
        annotation_id="ann",
        thread_generation=1,
        body=name,
        intent="ask_agent",
        anchor_revision_id=f"rev-{name}",
        capture_digest=None,
        reply_to_message_id=None,
        context_digest=_digest(name),
        client_request_id=name,
        actor_id="actor",
        turn_id=f"turn-{name}",
    )


def test_cancel_queued_is_terminal_and_retry_refuses(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "queued")
    assert repo.cancel_turn(accepted.turn_id, cancelled_at=10) == "cancelled"
    assert repo.cancel_turn(accepted.turn_id, cancelled_at=11) == "cancelled"
    turn = repo.turn(accepted.turn_id)
    assert turn["status"] == "cancelled"
    assert repo.claim_next_turn(
        annotation_id="ann", thread_generation=1, lease_owner="worker", lease_seconds=10
    ) is None
    with pytest.raises(RuntimeError, match="only failed"):
        repo.retry_turn(accepted.turn_id)


def test_cancel_running_revokes_completion_authority(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "running")
    claim = repo.claim_next_turn(
        annotation_id="ann", thread_generation=1, lease_owner="worker", lease_seconds=30
    )
    repo.mark_dispatched(
        claim.turn_id, lease_owner="worker", attempt=claim.attempt
    )
    assert repo.cancel_turn(accepted.turn_id) == "cancelled"
    with pytest.raises(RuntimeError, match="not held"):
        repo.complete_turn(
            turn_id=claim.turn_id,
            lease_owner="worker",
            attempt=claim.attempt,
            assistant_body="late answer",
            context_digest=_digest("answer"),
            actor_id="agent",
        )


def test_retry_reuses_same_turn_and_refuses_outcome_unknown(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "retry")
    claim = repo.claim_next_turn(
        annotation_id="ann", thread_generation=1, lease_owner="worker", lease_seconds=30
    )
    repo.fail_turn(
        claim.turn_id,
        lease_owner="worker",
        attempt=claim.attempt,
        error_code="provider_unavailable",
    )
    assert repo.retry_turn(accepted.turn_id) == 1
    assert repo.turn(accepted.turn_id)["status"] == "queued"

    second = _submit(repo, "unknown")
    # FIFO: settle the retried predecessor before claiming the second turn.
    retry_claim = repo.claim_next_turn(
        annotation_id="ann", thread_generation=1, lease_owner="retry", lease_seconds=1, now=1
    )
    repo.cancel_turn(retry_claim.turn_id, cancelled_at=1.5)
    unknown_claim = repo.claim_next_turn(
        annotation_id="ann", thread_generation=1, lease_owner="worker", lease_seconds=1, now=2
    )
    repo.mark_dispatched(
        unknown_claim.turn_id, lease_owner="worker", attempt=unknown_claim.attempt, now=2
    )
    repo.recover_expired_turns(now=4)
    assert repo.turn(second.turn_id)["dispatch_state"] == "outcome_unknown"
    with pytest.raises(RuntimeError, match="reconciled"):
        repo.retry_turn(second.turn_id)


def test_registry_coalesces_wakeups_and_drains_fifo(tmp_path):
    repo = _repo(tmp_path)
    first = _submit(repo, "one")
    second = _submit(repo, "two")
    calls = []
    terminal = []

    class Worker:
        def run_next(self, annotation_id, generation, *, cancellation_event=None):
            claim = repo.claim_next_turn(
                annotation_id=annotation_id,
                thread_generation=generation,
                lease_owner="registry",
                lease_seconds=30,
            )
            if claim is None:
                return AnnotationWorkerResult(claimed=False)
            calls.append(claim.turn_id)
            repo.cancel_turn(claim.turn_id)
            return AnnotationWorkerResult(
                claimed=True, turn_id=claim.turn_id, status="cancelled"
            )

    registry = AnnotationWorkerRegistry(repo, Worker, terminal_callback=terminal.append)
    registry.notify("ann", 1)
    registry.notify("ann", 1)
    deadline = time.time() + 2
    while len(terminal) < 2 and time.time() < deadline:
        time.sleep(0.01)
    registry.close()
    assert calls == [first.turn_id, second.turn_id]
    assert [item.status for item in terminal] == ["cancelled", "cancelled"]


def test_registry_reaps_idle_lane_without_losing_later_notify(tmp_path):
    repo = _repo(tmp_path)

    class Worker:
        def run_next(self, _annotation_id, _generation, *, cancellation_event=None):
            return AnnotationWorkerResult(claimed=False)

    registry = AnnotationWorkerRegistry(repo, Worker, idle_seconds=0.03)
    registry.notify("ann", 1)
    deadline = time.time() + 1
    while registry.active_lineages() and time.time() < deadline:
        time.sleep(0.01)
    assert registry.active_lineages() == ()
    registry.notify("ann", 1)
    assert registry.active_lineages() == (("ann", 1),)
    registry.close()


def test_notify_racing_post_timeout_is_consumed_not_reaped(tmp_path):
    repo = _repo(tmp_path)
    stop = threading.Event()
    cancellation = threading.Event()
    calls = []

    class Worker:
        def run_next(self, _annotation_id, _generation, *, cancellation_event=None):
            calls.append("drained")
            stop.set()
            return AnnotationWorkerResult(claimed=False)

    registry = AnnotationWorkerRegistry(repo, Worker, idle_seconds=1)

    class RacingWake:
        def __init__(self):
            self.flag = False
            self.raced = False

        def wait(self, timeout=None):
            return False

        def is_set(self):
            if not self.raced:
                self.raced = True
                registry.notify("ann", 1)
            return self.flag

        def set(self):
            self.flag = True

        def clear(self):
            self.flag = False

    wake = RacingWake()
    current = threading.current_thread()
    event_wake = cast(threading.Event, wake)
    registry._lanes[("ann", 1)] = _Lane(
        wake=event_wake, stop=stop, cancellation=cancellation, thread=current
    )

    registry._run_lane(("ann", 1), event_wake, stop, cancellation)

    assert wake.raced is True
    assert calls == ["drained"]


def test_registry_cancel_signals_inflight_worker_and_fences_turn(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "inflight")
    entered = threading.Event()
    interrupted = threading.Event()

    class Worker:
        def run_next(self, annotation_id, generation, *, cancellation_event=None):
            claim = repo.claim_next_turn(
                annotation_id=annotation_id,
                thread_generation=generation,
                lease_owner="registry",
                lease_seconds=30,
            )
            if claim is None:
                return AnnotationWorkerResult(claimed=False)
            entered.set()
            assert cancellation_event.wait_for_turn(claim.turn_id, timeout=1)
            interrupted.set()
            return AnnotationWorkerResult(
                claimed=True, turn_id=claim.turn_id, status="cancelled"
            )

    registry = AnnotationWorkerRegistry(repo, Worker)
    registry.notify("ann", 1)
    assert entered.wait(timeout=1)
    assert registry.cancel(accepted.turn_id) == "cancelled"
    assert interrupted.wait(timeout=1)
    assert repo.turn(accepted.turn_id)["status"] == "cancelled"
    registry.close()


def test_cancelling_queued_turn_does_not_interrupt_running_predecessor(tmp_path):
    repo = _repo(tmp_path)
    first = _submit(repo, "first")
    second = _submit(repo, "second")
    entered = threading.Event()
    release = threading.Event()
    interrupted = threading.Event()

    class Worker:
        def run_next(self, annotation_id, generation, *, cancellation_event=None):
            claim = repo.claim_next_turn(
                annotation_id=annotation_id,
                thread_generation=generation,
                lease_owner="registry",
                lease_seconds=30,
            )
            if claim is None:
                return AnnotationWorkerResult(claimed=False)
            entered.set()
            while not release.wait(0.01):
                if cancellation_event.is_turn_cancelled(claim.turn_id):
                    interrupted.set()
                    break
            return AnnotationWorkerResult(
                claimed=True, turn_id=claim.turn_id, status="failed"
            )

    registry = AnnotationWorkerRegistry(repo, Worker)
    registry.notify("ann", 1)
    assert entered.wait(timeout=1)
    assert registry.cancel(second.turn_id) == "cancelled"
    assert not interrupted.wait(timeout=0.1)
    assert repo.turn(first.turn_id)["status"] == "running"
    release.set()
    registry.close()


def test_periodic_recovery_handles_lease_expiring_after_startup(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "late-expiry")
    claim = repo.claim_next_turn(
        annotation_id="ann",
        thread_generation=1,
        lease_owner="vanished",
        lease_seconds=0.05,
    )
    assert claim is not None
    ran = threading.Event()

    class Worker:
        def run_next(self, annotation_id, generation, *, cancellation_event=None):
            current = repo.claim_next_turn(
                annotation_id=annotation_id,
                thread_generation=generation,
                lease_owner="recovered",
                lease_seconds=30,
            )
            if current is None:
                return AnnotationWorkerResult(claimed=False)
            ran.set()
            repo.cancel_turn(current.turn_id)
            return AnnotationWorkerResult(
                claimed=True, turn_id=current.turn_id, status="cancelled"
            )

    registry = AnnotationWorkerRegistry(repo, Worker, recovery_seconds=0.02)
    registry.recover_and_start()
    assert ran.wait(timeout=1)
    assert repo.turn(accepted.turn_id)["attempt"] == 1
    registry.close()


def test_cancel_losing_to_failure_does_not_poison_same_id_retry(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "retry-race")
    claim = repo.claim_next_turn(
        annotation_id="ann",
        thread_generation=1,
        lease_owner="failed-worker",
        lease_seconds=30,
    )
    repo.fail_turn(
        claim.turn_id,
        lease_owner="failed-worker",
        attempt=claim.attempt,
        error_code="provider_unavailable",
    )

    class Worker:
        def run_next(self, _annotation_id, _generation, *, cancellation_event=None):
            return AnnotationWorkerResult(claimed=False)

    registry = AnnotationWorkerRegistry(repo, Worker)
    registry.notify("ann", 1)
    assert registry.cancel(accepted.turn_id) == "failed"
    lane = registry._lanes[("ann", 1)]
    assert not lane.cancellation.is_turn_cancelled(accepted.turn_id)
    assert registry.retry(accepted.turn_id) == 1
    assert not lane.cancellation.is_turn_cancelled(accepted.turn_id)
    registry.close()


def test_mark_orphaned_cancels_pending_and_refuses_new_work(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "orphan")
    repo.mark_orphaned("ann", 1)
    assert repo.turn(accepted.turn_id)["status"] == "cancelled"
    with pytest.raises(RuntimeError, match="orphaned"):
        _submit(repo, "later")