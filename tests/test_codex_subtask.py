from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

from agent.codex_subtask.registry import JobRegistry
from agent.codex_subtask import supervisor as sup


@dataclass
class FakeTurnResult:
    final_text: str = "fake done"
    projected_messages: list[dict] = field(default_factory=lambda: [{"role": "assistant", "content": "fake done"}])
    tool_iterations: int = 1
    interrupted: bool = False
    error: str | None = None
    thread_id: str = "thread_fake"


class FakeSession:
    sleep_seconds = 0.0
    result = FakeTurnResult()
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.interrupted = False
        FakeSession.instances.append(self)

    def ensure_started(self):
        return "thread_fake"

    def run_turn(self, prompt, turn_timeout=600):
        self.prompt = prompt
        self.turn_timeout = turn_timeout
        deadline = time.time() + min(FakeSession.sleep_seconds, turn_timeout + 0.2)
        while time.time() < deadline:
            if self.interrupted:
                return FakeTurnResult(final_text="", interrupted=True, error="cancelled")
            time.sleep(0.01)
        return FakeSession.result

    def request_interrupt(self):
        self.interrupted = True

    def close(self):
        self.closed = True



def test_minimal_reasoning_is_normalized_for_codex():
    overrides = sup._overrides(
        model=None,
        reasoning_effort="minimal",
        sandbox_mode=None,
        allow_plugins=None,
        deny_plugins=None,
        skills=None,
    )
    assert 'model_reasoning_effort="low"' in overrides
    assert 'web_search="disabled"' in overrides
    assert 'features.image_generation=false' in overrides
    assert 'model_reasoning_effort="minimal"' not in overrides


def test_registry_restart_marks_live_jobs(tmp_path):
    reg = JobRegistry(tmp_path / "jobs.db")
    job = reg.create_job(prompt="p", cwd=str(tmp_path), profile="general", model=None, reasoning_effort=None, sandbox_mode=None, timeout_seconds=10)
    reg.update(job.job_id, status="running")
    assert reg.mark_active_interrupted_on_startup() == 1
    updated = reg.get(job.job_id)
    assert updated.status == "interrupted_by_restart"
    assert updated.error_text == "supervisor restarted"


def test_sync_submit_completes_and_echoes_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(sup, "CodexAppServerSession", FakeSession)
    FakeSession.sleep_seconds = 0
    FakeSession.result = FakeTurnResult(final_text="ok")
    s = sup.Supervisor(tmp_path / "jobs.db")
    resp = s.dispatch({"action": "submit", "mode": "sync", "prompt": "Say ok", "cwd": str(tmp_path), "timeout_seconds": 999})
    assert resp["status"] == "completed"
    assert resp["final_text"] == "ok"
    assert FakeSession.instances[-1].kwargs["codex_profile"] is None
    assert FakeSession.instances[-1].kwargs["startup_timeout_seconds"] == sup.STARTUP_TIMEOUT_SECONDS
    assert resp["timeout_seconds"] == 600
    assert "clamped" in resp["message"]
    assert FakeSession.instances[-1].turn_timeout == 600


def test_async_lifecycle_status_logs_and_await(monkeypatch, tmp_path):
    monkeypatch.setattr(sup, "CodexAppServerSession", FakeSession)
    FakeSession.sleep_seconds = 0.05
    FakeSession.result = FakeTurnResult(final_text="async ok")
    s = sup.Supervisor(tmp_path / "jobs.db")
    queued = s.dispatch({"action": "submit", "mode": "async", "prompt": "work", "cwd": str(tmp_path)})
    assert queued["status"] == "queued"
    job_id = queued["job_id"]
    done = s.dispatch({"action": "await", "job_id": job_id, "timeout_seconds": 2})
    assert done["status"] == "completed"
    assert done["final_text"] == "async ok"
    logs = s.dispatch({"action": "logs", "job_id": job_id})
    assert logs["status"] == "ok"
    assert logs["events"]
    listed = s.dispatch({"action": "list", "limit": 5})
    assert any(j["job_id"] == job_id for j in listed["jobs"])


def test_cancel_running_job(monkeypatch, tmp_path):
    monkeypatch.setattr(sup, "CodexAppServerSession", FakeSession)
    FakeSession.sleep_seconds = 5
    FakeSession.result = FakeTurnResult(final_text="late")
    s = sup.Supervisor(tmp_path / "jobs.db")
    queued = s.dispatch({"action": "submit", "mode": "async", "prompt": "sleep", "cwd": str(tmp_path), "timeout_seconds": 10})
    job_id = queued["job_id"]
    time.sleep(0.1)
    cancel = s.dispatch({"action": "cancel", "job_id": job_id})
    assert cancel["status"] in {"starting", "running", "cancelled", "interrupted"}
    done = s.dispatch({"action": "await", "job_id": job_id, "timeout_seconds": 2})
    assert done["status"] in {"cancelled", "error", "interrupted"}


def test_send_during_running_turn_fails_fast_and_cancel_still_works(monkeypatch, tmp_path):
    monkeypatch.setattr(sup, "CodexAppServerSession", FakeSession)
    FakeSession.sleep_seconds = 5
    FakeSession.result = FakeTurnResult(final_text="late")
    s = sup.Supervisor(tmp_path / "jobs.db")
    queued = s.dispatch({"action": "submit", "mode": "async", "prompt": "sleep", "cwd": str(tmp_path), "timeout_seconds": 10})
    job_id = queued["job_id"]
    time.sleep(0.1)

    started = time.monotonic()
    sent = s.dispatch({"action": "send", "job_id": job_id, "message": "change course"})
    assert time.monotonic() - started < 0.5
    assert sent["status"] == "error"
    assert "mid-turn send is not supported" in sent["error"]

    started = time.monotonic()
    cancel = s.dispatch({"action": "cancel", "job_id": job_id})
    assert time.monotonic() - started < 0.5
    assert cancel["status"] in {"starting", "running", "cancelled", "interrupted"}
    done = s.dispatch({"action": "await", "job_id": job_id, "timeout_seconds": 2})
    assert done["status"] in {"cancelled", "error", "interrupted"}



def test_registry_handles_concurrent_reads_and_writes(tmp_path):
    reg = JobRegistry(tmp_path / "jobs.db")
    job = reg.create_job(
        prompt="p",
        cwd=str(tmp_path),
        profile="general",
        model=None,
        reasoning_effort=None,
        sandbox_mode=None,
        timeout_seconds=10,
    )

    def work(i: int):
        if i % 3 == 0:
            updated = reg.update(job.job_id, tool_iterations=i)
            assert updated is not None
            return updated.tool_iterations
        if i % 3 == 1:
            found = reg.get(job.job_id)
            assert found is not None
            return found.job_id
        return len(reg.list(limit=5))

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(work, range(120)))

    assert results
    found = reg.get(job.job_id)
    assert found is not None
    assert found.job_id == job.job_id


def test_registry_serializes_concurrent_transcript_appends(tmp_path):
    reg = JobRegistry(tmp_path / "jobs.db")
    job = reg.create_job(
        prompt="p",
        cwd=str(tmp_path),
        profile="general",
        model=None,
        reasoning_effort=None,
        sandbox_mode=None,
        timeout_seconds=10,
    )

    def append(i: int) -> None:
        reg.append_transcript(job.job_id, {"i": i})

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(append, range(120)))

    events, cursor = reg.read_transcript(job.job_id, limit=200)
    assert cursor == 120
    assert sorted(event["i"] for event in events) == list(range(120))


def test_supervisor_concurrent_async_submits_do_not_corrupt_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(sup, "CodexAppServerSession", FakeSession)
    FakeSession.sleep_seconds = 0.01
    FakeSession.result = FakeTurnResult(final_text="ok")
    s = sup.Supervisor(tmp_path / "jobs.db")

    def submit(i: int):
        queued = s.dispatch(
            {"action": "submit", "mode": "async", "prompt": f"work {i}", "cwd": str(tmp_path)}
        )
        assert queued["status"] == "queued"
        return queued["job_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        job_ids = list(pool.map(submit, range(32)))

    done = [s.dispatch({"action": "await", "job_id": job_id, "timeout_seconds": 3}) for job_id in job_ids]
    assert all(resp["status"] == "completed" for resp in done)
    assert all("InterfaceError" not in str(resp) for resp in done)


def test_handler_ignores_broken_pipe_on_response_write():
    class BrokenWfile:
        def write(self, data):
            raise BrokenPipeError()

    handler = cast(Any, object.__new__(sup.Handler))
    handler.rfile = io.BytesIO(b'{"action":"ping"}\n')
    handler.wfile = BrokenWfile()
    handler.server = SimpleNamespace(
        supervisor=SimpleNamespace(dispatch=lambda req: {"status": "ok"})
    )

    handler.handle()


def test_empty_prompt_and_bad_job_id(tmp_path):
    s = sup.Supervisor(tmp_path / "jobs.db")
    assert s.dispatch({"action": "submit", "prompt": ""})["status"] == "error"
    assert s.dispatch({"action": "status", "job_id": "missing"})["status"] == "error"
