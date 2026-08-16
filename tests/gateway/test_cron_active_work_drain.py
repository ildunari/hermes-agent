"""Tests for #60432: the gateway shutdown drain was structurally blind to
in-flight cron work. Cron jobs run through cron/scheduler.py's own thread
pool, entirely outside ``GatewayRunner._running_agents`` -- the dict every
other active-work check on this class reads. A shutdown (``/update``,
``/restart``, SIGUSR1 -- they all funnel through the same ``stop()``) could
report ``active_at_start=0`` and immediately kill tool subprocesses while a
cron job's terminal command was still running.

These tests cover the gateway side of the fix:
  - _active_cron_job_count() reads cron.scheduler's in-flight job set
  - _drain_active_agents() waits for cron work the same way it already
    waits for chat sessions
  - the final tool-subprocess kill marks any still-in-flight cron job
    interrupted

See tests/cron/test_shutdown_interrupt.py for the cron-side primitives
this relies on (get_running_job_ids, mark_running_jobs_interrupted).
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture(autouse=True)
def _reset_cron_running_set():
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()
    yield
    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()


def _make_async_noop():
    async def _noop(*args, **kwargs):
        return None

    return _noop


class TestActiveCronJobCount:
    def test_zero_when_no_cron_jobs_running(self):
        runner, _adapter = make_restart_runner()
        assert runner._active_cron_job_count() == 0


class TestDrainWaitsForCronWork:

    @pytest.mark.asyncio
    async def test_drain_waits_for_in_flight_cron_job(self):
        """Before this fix, a cron-only workload made active_at_start=0
        and the drain returned instantly -- this is the exact repro from
        the issue (a `sleep 1800` cron job in flight during /update)."""
        import cron.scheduler as sched

        runner, _adapter = make_restart_runner()
        sched._running_job_ids.add("job-1")

        async def finish_job():
            await asyncio.sleep(0.12)
            sched._running_job_ids.discard("job-1")

        task = asyncio.create_task(finish_job())
        _snapshot, timed_out = await runner._drain_active_agents(2.0)
        await task

        assert timed_out is False, (
            "drain must wait for the cron job to finish, not report "
            "active_at_start=0 and return instantly"
        )


class TestCronPersistsActiveAgentsOnClaimRelease:
    """Regression coverage for docs/local/UPDATE_INCIDENTS_20260723.md item 12:
    cron dispatch/completion mutates ``_running_job_ids`` from a worker thread,
    entirely outside GatewayRunner, and previously never told the gateway to
    persist ``active_agents`` — leaving ``gateway_state.json`` stale until an
    unrelated turn boundary. ``_submit_with_guard`` must now persist on claim
    (job dispatched), on release (job finishes), and on the dispatch-failure
    release path."""

    def test_persists_on_claim_and_release(self, monkeypatch):
        import cron.scheduler as sched

        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()

        job = {
            "id": "persist-job",
            "name": "persist-test",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        calls = []
        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "run_job", lambda j, **_kw: (True, "out", "resp", None))
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: "/tmp/out")
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        with patch("gateway.run.persist_active_agents_now", side_effect=lambda: calls.append(1)):
            n = sched.tick(verbose=False)

        assert n == 1
        # Claim (dispatch) + release (worker finally) == at least 2 calls.
        assert len(calls) >= 2
        assert "persist-job" not in sched._running_job_ids

        sched._shutdown_parallel_pool()

    def test_persists_on_dispatch_failure_release(self, monkeypatch):
        """The ``pool.submit`` exception path releases the claim too — it
        must persist on that release path just like the normal one."""
        import cron.scheduler as sched

        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()

        job = {
            "id": "dispatch-fail-job",
            "name": "dispatch-fail-test",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        calls = []
        pool = MagicMock()
        pool.submit.side_effect = RuntimeError("executor dispatch boom")
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda *_a, **_kw: pool)
        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "finish_execution", lambda *_a, **_kw: None)

        with patch("gateway.run.persist_active_agents_now", side_effect=lambda: calls.append(1)):
            sched.tick(verbose=False)

        # Claim + dispatch-failure release == at least 2 calls, and the
        # claim must not be left dangling in _running_job_ids.
        assert len(calls) >= 2
        assert "dispatch-fail-job" not in sched._running_job_ids

        sched._shutdown_parallel_pool()

    def test_persist_failure_never_breaks_dispatch(self, monkeypatch):
        """Best-effort: a broken persist hook must not stop the job from
        running or from being released from the running set."""
        import cron.scheduler as sched

        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()

        job = {
            "id": "persist-broken-job",
            "name": "persist-broken-test",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "run_job", lambda j, **_kw: (True, "out", "resp", None))
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: "/tmp/out")
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        with patch("gateway.run.persist_active_agents_now", side_effect=RuntimeError("boom")):
            n = sched.tick(verbose=False)

        assert n == 1
        assert "persist-broken-job" not in sched._running_job_ids

        sched._shutdown_parallel_pool()


class TestKillToolSubprocessesMarksCronInterrupted:
    @pytest.mark.asyncio
    async def test_in_flight_cron_job_marked_interrupted_on_forced_kill(self, monkeypatch):
        import cron.scheduler as sched
        import tools.process_registry as _pr
        import tools.terminal_tool as _tt
        import tools.browser_tool as _bt

        runner, adapter = make_restart_runner()
        runner._restart_drain_timeout = 0.01  # force the timeout path
        runner._cron_drain_timeout = 0.01  # ...past the cron floor too (#82161)
        adapter.disconnect = _make_async_noop()

        sched._running_job_ids.add("job-1")
        sched._running_fire_owners["job-1"] = {
            object(): ("owner-1", sched._get_hermes_home().resolve())
        }

        monkeypatch.setattr(_pr.process_registry, "kill_all", lambda task_id=None: 1)
        monkeypatch.setattr(_tt, "cleanup_all_environments", lambda: None)
        monkeypatch.setattr(_bt, "cleanup_all_browsers", lambda: None)

        marked_calls = []
        real_mark = sched.mark_running_jobs_interrupted

        def _spy(reason):
            result = real_mark(reason)
            marked_calls.append((reason, result))
            return result

        monkeypatch.setattr(sched, "mark_running_jobs_interrupted", _spy)

        with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"), \
             patch("cron.scheduler.mark_job_run"):
            await runner.stop()

        assert marked_calls, "mark_running_jobs_interrupted was never called during shutdown"
        assert any(result == ["job-1"] for _reason, result in marked_calls)

