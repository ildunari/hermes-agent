"""Detached authority, real dispatch and process cleanup contracts."""
import dataclasses
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent.subagent_lifecycle import SubagentLifecycleService, SubagentLaunchRequest, SubagentLifecycleError, SubagentState
from agent import subagent_lifecycle_detached as detached


@pytest.fixture
def lane(monkeypatch, tmp_path):
    manager = detached.DetachedLifecycle()
    monkeypatch.setattr(detached, "manager", manager)
    service = SubagentLifecycleService(lambda: None)
    release = threading.Event()
    children = []
    results = []
    identity = detached.SubagentSessionIdentity(str(tmp_path), "saved-parent", "contact-a", "guest-a")

    @contextmanager
    def factory():
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from gateway.conversation_extensions import request_policy_scope, issue_request_policy
        token = set_hermes_home_override(identity.profile_home)
        try:
            with request_policy_scope(issue_request_policy(extension_id="test", profile_home=identity.profile_home, route_id="test")):
                yield SimpleNamespace(session_id=identity.session_id, enabled_toolsets=["file"])
        finally:
            reset_hermes_home_override(token)

    def build(parent, request):
        child = SimpleNamespace(provider="test", model=request.model, stopped=threading.Event(), steers=[])
        child.hard_interrupt = lambda *a, **k: child.stopped.set()
        child.steer = lambda text: child.steers.append(text) or True
        children.append(child)
        return child

    def run(index, goal, child, parent):
        while not release.wait(.005) and not child.stopped.is_set():
            pass
        return {"status": "interrupted" if child.stopped.is_set() else "completed", "summary": goal}

    monkeypatch.setattr(detached, "build_child", build)
    monkeypatch.setattr("tools.delegate_tool._run_child_lifecycle", run)
    binding = service.bind_session(identity, parent_factory=factory,
        notification_target=detached.SilentNotificationTarget(identity, results.append))
    yield service, binding, children, results, release, manager
    release.set()
    for child in children:
        child.stopped.set()


def wait_for(predicate):
    until = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < until
        time.sleep(.005)


def request(**kw):
    return SubagentLaunchRequest(goal="work", job_id="job", deadline_at=time.time() + 60, **kw)


def test_detached_binding_cannot_be_forged_or_cross_contact_and_needs_no_active_parent(lane):
    service, binding, children, results, release, manager = lane
    handle = service.detached_launch(binding, request())
    wait_for(lambda: children)
    changed = dataclasses.replace(binding, identity=dataclasses.replace(binding.identity, scope_id="contact-b"))
    with pytest.raises(SubagentLifecycleError):
        service.detached_status(changed, handle)
    with pytest.raises(SubagentLifecycleError):
        service.detached_launch(dataclasses.replace(binding, authority_token="forged"), request())
    assert service.detached_steer(binding, handle, text="new context")
    assert children[0].steers == ["new context"]
    release.set()
    wait_for(lambda: results)
    assert service.detached_result(binding, handle).summary == "work"
    assert service.detached_status(binding, handle).state == SubagentState.SUCCEEDED


def test_cap_is_per_contact_across_jobs_and_deadline_cannot_be_reset(lane):
    service, binding, children, results, release, manager = lane
    req = request()
    handles = [service.detached_launch(binding, dataclasses.replace(req, job_id=f"job-{i}")) for i in range(10)]
    with pytest.raises(SubagentLifecycleError, match="ten active"):
        service.detached_launch(binding, dataclasses.replace(req, job_id="eleventh"))
    with pytest.raises(SubagentLifecycleError, match="cannot change"):
        service.detached_launch(binding, dataclasses.replace(req, job_id="job-0", deadline_at=req.deadline_at + 1))
    other_identity = dataclasses.replace(binding.identity, scope_id="other", principal="other")
    other = service.bind_session(other_identity, parent_factory=manager.binding(binding).factory,
        notification_target=detached.SilentNotificationTarget(other_identity, results.append))
    other_handle = service.detached_launch(other, req)
    with pytest.raises(SubagentLifecycleError, match="not owned"):
        service.detached_result(other, handles[0])
    assert service.detached_status(other, other_handle).state != SubagentState.UNKNOWN


def test_absolute_deadline_interrupts_and_final_dispatch_denies_blocked_tools(lane, monkeypatch):
    service, binding, children, results, release, manager = lane
    req = dataclasses.replace(request(blocked_tools=("read_file",)), deadline_at=time.time() + .15)
    handle = service.detached_launch(binding, req)
    wait_for(lambda: children)
    from model_tools import handle_function_call
    assert "blocked by detached" in handle_function_call("read_file", {"path": "/does-not-exist"}, task_id=handle.subagent_id)
    wait_for(lambda: results)
    assert children[0].stopped.is_set()
    assert service.detached_result(binding, handle).terminal_state == SubagentState.CANCELLED
    assert "ended" in handle_function_call("terminal", {"command": "never run"}, task_id=handle.subagent_id)


def test_cancel_kills_only_worker_owned_background_process(lane, tmp_path):
    service, binding, children, results, release, manager = lane
    handle = service.detached_launch(binding, request())
    wait_for(lambda: children)
    from tools.process_registry import process_registry
    owned = process_registry.spawn_local("sleep 30", task_id=handle.subagent_id, cwd=str(tmp_path))
    other = process_registry.spawn_local("sleep 30", task_id="unrelated-detached-test", cwd=str(tmp_path))
    try:
        service.detached_cancel(binding, handle, reason="obsolete")
        wait_for(lambda: owned.exited)
        assert not other.exited
        wait_for(lambda: results)
        assert service.detached_result(binding, handle).terminal_state == SubagentState.CANCELLED
    finally:
        process_registry.kill_all("unrelated-detached-test", source="test cleanup")


@pytest.mark.parametrize("provider,base_url,effort", [
    ("custom", "http://127.0.0.1:10110/v1", "xhigh"),
    ("openai-codex", None, "high"),
])
def test_real_delegate_constructor_receives_provider_personality_and_reasoning(monkeypatch, provider, base_url, effort):
    # Exercise the public route resolver and the actual delegate constructor,
    # replacing only the external runtime resolution and AIAgent allocation.
    from agent.subagent_lifecycle_detached import build_child
    import tools.delegate_tool as delegate
    seen = {}
    runtime = {"provider": provider, "base_url": base_url or "https://chatgpt.com/backend-api/codex",
               "api_key": "endpoint-only", "api_mode": "chat_completions",
               "request_overrides": {"extra_body": {"route": "presentation"}}}
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: runtime)
    monkeypatch.setattr(delegate, "_open_child_session_db", lambda p: None)
    monkeypatch.setattr(delegate, "_resolve_child_credential_pool", lambda *a: None)
    monkeypatch.setattr(delegate, "_load_config", lambda: {"reasoning_effort": "low"})
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: None)
    def allocate(**kw):
        seen.update(kw)
        return SimpleNamespace(**kw)
    monkeypatch.setattr("run_agent.AIAgent", allocate)
    parent = SimpleNamespace(session_id="saved", model="parent-model", provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex", api_key="parent-only", enabled_toolsets=["file"])
    child = build_child(parent, request(model="presentation-model", provider=provider,
        base_url=base_url, reasoning_effort=effort))
    assert seen["api_key"] == "endpoint-only"
    assert seen["provider"] == provider
    assert seen["model"] == "presentation-model"
    assert seen["request_overrides"] == runtime["request_overrides"]
    from hermes_constants import parse_reasoning_effort
    assert seen["reasoning_config"] == parse_reasoning_effort(effort)
    assert parent.api_key == "parent-only"


def test_factory_without_restored_authority_fails_before_construction(lane):
    service, binding, children, results, release, manager = lane
    @contextmanager
    def unscoped():
        yield SimpleNamespace(session_id=binding.identity.session_id)
    invalid = service.bind_session(binding.identity, parent_factory=unscoped,
        notification_target=detached.SilentNotificationTarget(binding.identity, results.append))
    handle = service.detached_launch(invalid, request())
    wait_for(lambda: results)
    assert not children
    assert service.detached_result(invalid, handle).terminal_state == SubagentState.FAILED
    assert "scope mismatch" in results[0].error_message


def test_real_delegate_worker_keeps_profile_policy_silence_and_owns_timeout(monkeypatch, tmp_path):
    from tools.delegate_tool_child_run import _ChildRun
    from gateway.conversation_extensions import issue_request_policy, request_policy_scope, current_request_policy
    from gateway.session_context import declare_stateless_channel, async_delivery_supported
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override, hermes_home_key
    seen = []
    def conversation(**kwargs):
        time.sleep(.05)
        seen.append((hermes_home_key(), current_request_policy().route_id, async_delivery_supported()))
        return {"final_response": "done"}
    child = SimpleNamespace(session_id="child", _lifecycle_owns_deadline=True, run_conversation=conversation)
    monkeypatch.setattr("tools.delegate_tool._get_child_timeout", lambda: .001)
    monkeypatch.setattr("tools.delegate_tool._get_subagent_approval_callback", lambda: None)
    token = set_hermes_home_override(str(tmp_path))
    try:
        with request_policy_scope(issue_request_policy(extension_id="test", profile_home=str(tmp_path), route_id="authenticated")):
            # A copy avoids altering the test thread's subsequent session context.
            import contextvars
            def run():
                declare_stateless_channel()
                worker = _ChildRun(child, None, 0, "work", "child", None)
                return worker.await_child()
            result, error, deferred = contextvars.copy_context().run(run)
        assert result["final_response"] == "done" and error is None and not deferred
        assert seen == [(str(tmp_path.resolve()), "authenticated", False)]
    finally:
        reset_hermes_home_override(token)
