"""Regression coverage for #63529 API-server shutdown draining.

API-server work is adapter-owned rather than tracked by
``GatewayRunner._running_agents``. The shutdown drain must account for the
same live state as the API concurrency limiter, including a ``/v1/runs`` task
that exists before its agent has been constructed, and it must refuse new API
turns once the gateway starts draining.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tests.gateway.restart_test_helpers import make_restart_runner


class _RunTask:
    def __init__(self, done: bool = False):
        self._done = done

    def done(self) -> bool:
        return self._done


def _make_api_adapter(*, inflight: int = 0, queued_ids=()):
    tasks = {run_id: _RunTask() for run_id in queued_ids}
    adapter = SimpleNamespace(
        platform=Platform.API_SERVER,
        _inflight_agent_runs=inflight,
        _active_run_tasks=tasks,
    )

    def active_agent_work_count() -> int:
        return int(getattr(adapter, "_pending_agent_requests", 0)) + int(
            adapter._inflight_agent_runs
        ) + sum(not task.done() for task in adapter._active_run_tasks.values())

    adapter.active_agent_work_count = active_agent_work_count
    return adapter


def _make_admission_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream
    )
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_post("/v1/runs", adapter._handle_runs)
    return app


class TestActiveApiRunCount:
    def test_zero_when_no_api_adapter(self):
        runner, _adapter = make_restart_runner()
        runner.adapters = {}
        assert runner._active_api_run_count() == 0

    def test_delegates_to_primary_api_adapter(self):
        runner, _adapter = make_restart_runner()
        runner.adapters = {
            Platform.API_SERVER: _make_api_adapter(inflight=2, queued_ids=["r1"])
        }
        assert runner._active_api_run_count() == 3

    def test_ignores_non_api_platforms(self):
        runner, _adapter = make_restart_runner()
        other = SimpleNamespace(
            platform=Platform.DISCORD,
            active_agent_work_count=lambda: 99,
        )
        runner.adapters = {Platform.DISCORD: other}
        assert runner._active_api_run_count() == 0

    def test_raises_on_broken_adapter_never_false_idle(self):
        """Codex fix-lane review P1-3: a FAILED count is unknown, never idle.
        Swallowing a raise as 0 let a cron-thread race publish
        active_agents=0 while a live /v1/runs task was in flight."""
        runner, _adapter = make_restart_runner()

        class Bad:
            platform = Platform.API_SERVER

            @staticmethod
            def active_agent_work_count() -> int:
                raise RuntimeError("boom")

        runner.adapters = {Platform.API_SERVER: Bad()}
        with pytest.raises(RuntimeError):
            runner._active_api_run_count()

    def test_delegates_to_primary_api_adapter(self):
        runner, _adapter = make_restart_runner()
        runner.adapters = {
            Platform.API_SERVER: _make_api_adapter(inflight=2, queued_ids=["r1"])
        }
        assert runner._active_api_run_count() == 3

    def test_ignores_non_api_platforms(self):
        runner, _adapter = make_restart_runner()
        other = SimpleNamespace(
            platform=Platform.DISCORD,
            active_agent_work_count=lambda: 99,
        )
        runner.adapters = {Platform.DISCORD: other}
        assert runner._active_api_run_count() == 0

    def test_never_raises_on_broken_adapter(self):
        runner, _adapter = make_restart_runner()

        class Bad:
            platform = Platform.API_SERVER

            @staticmethod
            def active_agent_work_count() -> int:
                raise RuntimeError("boom")

        runner.adapters = {Platform.API_SERVER: Bad()}
        assert runner._active_api_run_count() == 0


class TestAPIServerAdapterWorkCount:

    @pytest.mark.asyncio
    async def test_concurrency_limit_excludes_current_pending_admission(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        adapter._max_concurrent_runs = 1
        app = _make_admission_app(adapter)

        async with TestClient(TestServer(app)) as client:
            with patch.object(adapter, "_run_agent", new=AsyncMock(return_value=({}, {}))):
                response = await client.post(
                    "/api/sessions/s/chat",
                    json={"message": "hello"},
                )

        assert response.status == 404


    def test_counts_live_run_task_before_agent_creation(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        adapter._inflight_agent_runs = 2
        adapter._active_run_tasks = {
            "queued": _RunTask(),
            "finished": _RunTask(done=True),
        }
        adapter._active_run_agents = {}

        assert adapter.active_agent_work_count() == 3


class TestDrainWaitsForApiWork:

    @pytest.mark.asyncio
    async def test_drain_waits_for_real_queued_run_before_agent_creation(self):
        """A live /v1/runs task must block drain before it has an agent."""
        runner, _adapter = make_restart_runner()
        api = APIServerAdapter(PlatformConfig(enabled=True))
        runner.adapters = {Platform.API_SERVER: api}
        app = _make_admission_app(api)
        original_create_task = asyncio.create_task
        task_started = asyncio.Event()
        allow_task = asyncio.Event()

        def delayed_create_task(coro):
            async def delayed():
                task_started.set()
                await allow_task.wait()
                return await coro

            return original_create_task(delayed())

        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "done"}
        mock_agent.session_prompt_tokens = 0
        mock_agent.session_completion_tokens = 0
        mock_agent.session_total_tokens = 0

        with patch(
            "gateway.platforms.api_server.asyncio.create_task",
            side_effect=delayed_create_task,
        ), patch.object(api, "_create_agent", return_value=mock_agent):
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/v1/runs", json={"input": "hello"})
                assert response.status == 202
                await task_started.wait()

                assert api._active_run_agents == {}
                assert runner._active_api_run_count() == 1
                drain_task = original_create_task(runner._drain_active_agents(2.0))
                await asyncio.sleep(0.1)
                assert not drain_task.done()

                allow_task.set()
                _snapshot, timed_out = await drain_task

        assert timed_out is False


class TestPersistsActiveAgentsOnClaimRelease:
    """Regression coverage for docs/local/UPDATE_INCIDENTS_20260723.md item 12:
    API-server claim/release paths mutate counters (``_pending_agent_requests``,
    ``_inflight_agent_runs``, ``_active_run_tasks``) that feed
    ``GatewayRunner._active_work_count()`` but previously never told the
    gateway to persist ``active_agents`` -- leaving ``gateway_state.json``
    stale until an unrelated turn boundary."""

    @pytest.mark.asyncio
    async def test_admission_wrapper_persists_on_claim_and_release(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        runner, _adapter = make_restart_runner()
        runner.adapters = {Platform.API_SERVER: adapter}
        app = _make_admission_app(adapter)
        calls = []

        with patch("gateway.run._gateway_runner_ref", lambda: runner), patch(
            "gateway.run.persist_active_agents_now", side_effect=lambda: calls.append(1)
        ), patch.object(
            adapter, "_get_existing_session_or_404", return_value=({}, None)
        ), patch.object(
            adapter, "_read_json_body", new=AsyncMock(return_value=({"message": "hi"}, None))
        ), patch.object(
            adapter, "_run_agent", new=AsyncMock(return_value=({"final_response": "ok"}, {}))
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/api/sessions/missing/chat", json={})

        assert response.status == 200
        # Claim (admission) + release (finally) == at least 2 calls.
        assert len(calls) >= 2

    @pytest.mark.asyncio
    async def test_v1_runs_persists_on_claim_and_teardown(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        runner, _adapter = make_restart_runner()
        runner.adapters = {Platform.API_SERVER: adapter}
        app = _make_admission_app(adapter)
        calls = []

        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "done"}
        mock_agent.session_prompt_tokens = 0
        mock_agent.session_completion_tokens = 0
        mock_agent.session_total_tokens = 0

        with patch("gateway.run._gateway_runner_ref", lambda: runner), patch(
            "gateway.run.persist_active_agents_now", side_effect=lambda: calls.append(1)
        ), patch.object(adapter, "_create_agent", return_value=mock_agent):
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/v1/runs", json={"input": "hello"})
                assert response.status == 202
                run_id = (await response.json())["run_id"]

                for _ in range(200):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.01)

        assert run_id not in adapter._active_run_tasks
        # Claim (task registration) + release (teardown finally) == at least 2.
        assert len(calls) >= 2

    @pytest.mark.asyncio
    async def test_persist_hook_failure_never_breaks_admission(self):
        """Best-effort: a broken persist hook must not fail the request."""
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        runner, _adapter = make_restart_runner()
        runner.adapters = {Platform.API_SERVER: adapter}
        app = _make_admission_app(adapter)

        with patch("gateway.run._gateway_runner_ref", lambda: runner), patch(
            "gateway.run.persist_active_agents_now", side_effect=RuntimeError("boom")
        ), patch.object(
            adapter, "_get_existing_session_or_404", return_value=({}, None)
        ), patch.object(
            adapter, "_read_json_body", new=AsyncMock(return_value=({"message": "hi"}, None))
        ), patch.object(
            adapter, "_run_agent", new=AsyncMock(return_value=({"final_response": "ok"}, {}))
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/api/sessions/missing/chat", json={})

        assert response.status == 200


class TestPersistActiveAgentsNowThrottle:
    """Unit coverage for gateway.run.persist_active_agents_now — the shared,
    throttled entry point api_server and cron use to persist active_agents
    from claim/release sites outside GatewayRunner (docs/local/
    UPDATE_INCIDENTS_20260723.md item 12)."""

    @pytest.fixture(autouse=True)
    def _clear_cron_running_set(self):
        import cron.scheduler as sched

        sched._running_job_ids.clear()
        yield
        sched._running_job_ids.clear()

    def _isolated_state(self, monkeypatch):
        import gateway.run as run_mod

        state = {"ts": 0.0, "count": -1.0}
        monkeypatch.setattr(run_mod, "_active_agents_persist_state", state)
        return run_mod, state

    def test_no_runner_is_a_silent_no_op(self, monkeypatch):
        import gateway.run as run_mod

        monkeypatch.setattr(run_mod, "_gateway_runner_ref", lambda: None)
        run_mod.persist_active_agents_now()  # must not raise

    def test_first_call_always_persists(self, monkeypatch):
        run_mod, _state = self._isolated_state(monkeypatch)
        runner, _adapter = make_restart_runner()
        runner._running_agents = {"s1": object()}
        monkeypatch.setattr(run_mod, "_gateway_runner_ref", lambda: runner)
        runner._update_runtime_status = MagicMock()  # unused by _persist_active_agents

        with patch("gateway.status.write_runtime_status") as write_mock:
            run_mod.persist_active_agents_now()

        write_mock.assert_called_once()
        passed = write_mock.call_args.kwargs["active_agents"]
        assert (passed() if callable(passed) else passed) == 1

    def test_repeat_calls_within_window_are_throttled(self, monkeypatch):
        run_mod, _state = self._isolated_state(monkeypatch)
        runner, _adapter = make_restart_runner()
        runner._running_agents = {"s1": object(), "s2": object()}
        monkeypatch.setattr(run_mod, "_gateway_runner_ref", lambda: runner)

        with patch("gateway.status.write_runtime_status") as write_mock:
            run_mod.persist_active_agents_now()
            run_mod.persist_active_agents_now()  # same count, immediately after

        assert write_mock.call_count == 1

    def test_transition_to_zero_always_persists_even_within_window(self, monkeypatch):
        """The exact case that matters: a release must never sit behind the
        throttle window, or a drain poller reads a stale nonzero count."""
        run_mod, _state = self._isolated_state(monkeypatch)
        runner, _adapter = make_restart_runner()
        runner._running_agents = {"s1": object()}
        monkeypatch.setattr(run_mod, "_gateway_runner_ref", lambda: runner)

        with patch("gateway.status.write_runtime_status") as write_mock:
            run_mod.persist_active_agents_now()  # count=1
            runner._running_agents.clear()  # release -> count=0
            run_mod.persist_active_agents_now()  # must NOT be throttled

        assert write_mock.call_count == 2
        passed = write_mock.call_args_list[-1].kwargs["active_agents"]
        assert (passed() if callable(passed) else passed) == 0

    def test_transition_from_zero_always_persists_even_within_window(self, monkeypatch):
        run_mod, _state = self._isolated_state(monkeypatch)
        runner, _adapter = make_restart_runner()
        monkeypatch.setattr(run_mod, "_gateway_runner_ref", lambda: runner)

        with patch("gateway.status.write_runtime_status") as write_mock:
            run_mod.persist_active_agents_now()  # count=0
            runner._running_agents = {"s1": object()}  # claim -> count=1
            run_mod.persist_active_agents_now()  # must NOT be throttled

        assert write_mock.call_count == 2
        passed = write_mock.call_args_list[-1].kwargs["active_agents"]
        assert (passed() if callable(passed) else passed) == 1

    def test_after_window_elapses_repeat_call_persists(self, monkeypatch):
        run_mod, state = self._isolated_state(monkeypatch)
        runner, _adapter = make_restart_runner()
        runner._running_agents = {"s1": object()}
        monkeypatch.setattr(run_mod, "_gateway_runner_ref", lambda: runner)

        with patch("gateway.status.write_runtime_status") as write_mock:
            run_mod.persist_active_agents_now()
            # Fast-forward the throttle clock past the window without a real
            # sleep.
            state["ts"] -= run_mod._ACTIVE_AGENTS_PERSIST_MIN_INTERVAL + 0.01
            run_mod.persist_active_agents_now()

        assert write_mock.call_count == 2


class TestDrainAdmission:
    @pytest.mark.asyncio
    async def test_drain_refuses_every_agent_start_endpoint(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        runner = SimpleNamespace(_draining=True, _external_drain_active=False)
        app = _make_admission_app(adapter)
        paths = (
            "/api/sessions/missing/chat",
            "/api/sessions/missing/chat/stream",
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/runs",
        )

        with patch("gateway.run._gateway_runner_ref", lambda: runner):
            async with TestClient(TestServer(app)) as client:
                for path in paths:
                    response = await client.post(path, json={})
                    payload = await response.json()

                    assert response.status == 503
                    assert response.headers["Retry-After"] == "1"
                    assert payload["error"]["code"] == "gateway_draining"


