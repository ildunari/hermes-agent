"""Lifecycle, generation-scoped cancellation, and production-integration tests.

These drive the real integration surface (``conversation_extension_runtime``)
and the real plugin registration path, not helper-only unit shims.
"""

from __future__ import annotations

import contextvars
import threading

import pytest

from gateway import conversation_extensions as ce
from gateway import conversation_extension_runtime as ce_runtime


SCOPE_A = "/home/profile-a"
SCOPE_B = "/home/profile-b"


@pytest.fixture(autouse=True)
def _clean():
    ce.conversation_extension_registry.reset_for_tests()
    ce.lifecycle_task_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    ce.reset_gateway_host_operations()
    yield
    ce.conversation_extension_registry.reset_for_tests()
    ce.lifecycle_task_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    ce.reset_gateway_host_operations()


class _FakeManager:
    def __init__(self, scope_key: str):
        self.scope_key = scope_key

    def _track_registration(self, manifest, kind, key, release, persistent=False):
        from hermes_cli.plugins import PluginRegistration

        return PluginRegistration(kind=kind, key=key, release=release)


def _context(scope: str):
    from hermes_cli.plugins import PluginContext

    context = PluginContext.__new__(PluginContext)
    context._manager = _FakeManager(scope)
    context.manifest = type("_M", (), {"name": "probe", "key": "probe"})()
    return context


class _CancellableTask:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


# ---------------------------------------------------------------------------
# lifecycle start/stop through the real registration path
# ---------------------------------------------------------------------------


def _lifecycle_bundle(events, *, fail_start=False, spawn_key=None):
    def _on_start(facade):
        events.append(("start", facade.extension_id, facade.generation))
        if spawn_key:
            facade.spawn_lifecycle_task(spawn_key, lambda: None)
        if fail_start:
            raise RuntimeError("start exploded")

    def _on_stop(facade):
        events.append(("stop", facade.extension_id, facade.generation))

    capabilities = {"lifecycle", "tool_authorization"}
    return ce.GatewayConversationExtension(
        extension_id="probe",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset(capabilities),
        on_start=_on_start,
        on_stop=_on_stop,
        authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
    )


def test_registration_calls_on_start_with_a_facade():
    events = []
    handle = _context(SCOPE_A).register_gateway_conversation_extension(
        _lifecycle_bundle(events)
    )
    assert handle is not None
    assert events[0][0] == "start"
    assert events[0][1] == "probe"


def test_unload_calls_on_stop_then_unregisters():
    events = []
    handle = _context(SCOPE_A).register_gateway_conversation_extension(
        _lifecycle_bundle(events)
    )
    handle.dispose()
    assert [item[0] for item in events] == ["start", "stop"]
    assert ce.conversation_extension_registry.get("probe", scope=SCOPE_A) is None


def test_failed_on_start_rolls_back_registration():
    """A half-started generation must never remain published."""
    events = []
    handle = _context(SCOPE_A).register_gateway_conversation_extension(
        _lifecycle_bundle(events, fail_start=True)
    )
    assert handle is None
    assert ce.conversation_extension_registry.get("probe", scope=SCOPE_A) is None


def test_failed_on_start_cancels_its_own_tasks():
    cancelled: list[tuple] = []
    ce.install_gateway_host_operations(
        ce.GatewayHostOperations(
            spawn_task=lambda task: None,
            cancel_tasks=lambda ext, home, gen: cancelled.append((ext, home, gen)),
        )
    )
    events = []
    handle = _context(SCOPE_A).register_gateway_conversation_extension(
        _lifecycle_bundle(events, fail_start=True, spawn_key="watcher")
    )
    assert handle is None
    assert cancelled, "rollback must cancel the failed generation's tasks"


def test_replacement_is_not_published_until_start_completes():
    """Concurrent readers see the old complete bundle while replacement starts."""
    entered = threading.Event()
    release = threading.Event()

    def _bundle(label, on_start=None):
        return ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset(
                {"tool_authorization"} | ({"lifecycle"} if on_start else set())
            ),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(
                True, reason=label
            ),
            on_start=on_start,
        )

    context = _context(SCOPE_A)
    old = _bundle("old")
    assert context.register_gateway_conversation_extension(old) is not None

    def _start(_facade):
        entered.set()
        assert release.wait(timeout=5)

    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            context.register_gateway_conversation_extension(_bundle("new", _start))
        )
    )
    thread.start()
    assert entered.wait(timeout=5)
    assert ce.conversation_extension_registry.get("probe", scope=SCOPE_A) is old

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result and result[0] is not None
    assert ce.conversation_extension_registry.get("probe", scope=SCOPE_A) is not old


def test_failed_replacement_start_preserves_previous_generation():
    context = _context(SCOPE_A)
    old = _lifecycle_bundle([])
    assert context.register_gateway_conversation_extension(old) is not None
    old_generation = ce.conversation_extension_registry.active_generation(
        "probe", scope=SCOPE_A
    )

    assert context.register_gateway_conversation_extension(
        _lifecycle_bundle([], fail_start=True)
    ) is None
    assert ce.conversation_extension_registry.get("probe", scope=SCOPE_A) is old
    assert ce.conversation_extension_registry.active_generation(
        "probe", scope=SCOPE_A
    ) == old_generation


def test_on_stop_failure_still_unregisters():
    def _on_stop(facade):
        raise RuntimeError("stop exploded")

    bundle = ce.GatewayConversationExtension(
        extension_id="probe",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization"}),
        authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
        on_stop=_on_stop,
    )
    handle = _context(SCOPE_A).register_gateway_conversation_extension(bundle)
    handle.dispose()
    assert ce.conversation_extension_registry.get("probe", scope=SCOPE_A) is None


# ---------------------------------------------------------------------------
# generation-scoped cancellation
# ---------------------------------------------------------------------------


def test_replacement_cancels_only_the_outgoing_generation():
    cancelled: list[tuple] = []
    ce.install_gateway_host_operations(
        ce.GatewayHostOperations(
            spawn_task=lambda task: None,
            cancel_tasks=lambda ext, home, gen: cancelled.append((ext, home, gen)),
        )
    )
    context = _context(SCOPE_A)
    events = []
    first = context.register_gateway_conversation_extension(_lifecycle_bundle(events))
    first_generation = ce.conversation_extension_registry.active_generation(
        "probe", scope=SCOPE_A
    )
    second = context.register_gateway_conversation_extension(_lifecycle_bundle(events))

    assert cancelled, "replacement must cancel the outgoing generation"
    assert cancelled[0] == ("probe", SCOPE_A, first_generation)
    assert second is not None


def test_stale_generation_teardown_cannot_cancel_newer_tasks():
    registry = ce.LifecycleTaskRegistry()
    old_task = _CancellableTask()
    new_task = _CancellableTask()
    registry.record(
        ce.GatewayBackgroundTask(("probe", SCOPE_A, "watcher", 1), lambda: None), old_task
    )
    registry.record(
        ce.GatewayBackgroundTask(("probe", SCOPE_A, "watcher", 2), lambda: None), new_task
    )

    registry.cancel("probe", SCOPE_A, 1)
    assert old_task.cancelled is True
    assert new_task.cancelled is False
    assert registry.active_generations("probe", SCOPE_A) == (2,)


def test_task_cancellation_is_profile_scoped():
    registry = ce.LifecycleTaskRegistry()
    task_a = _CancellableTask()
    task_b = _CancellableTask()
    registry.record(
        ce.GatewayBackgroundTask(("probe", SCOPE_A, "w", 1), lambda: None), task_a
    )
    registry.record(
        ce.GatewayBackgroundTask(("probe", SCOPE_B, "w", 1), lambda: None), task_b
    )

    registry.cancel("probe", SCOPE_A, 1)
    assert task_a.cancelled is True
    assert task_b.cancelled is False


def test_spawn_fails_closed_without_host_task_runner():
    """An extension must never believe it started a watcher that nothing runs."""
    facade = ce.GatewayRuntimeFacade(
        extension_id="probe",
        profile_name="a",
        profile_home=SCOPE_A,
        generation=1,
        capabilities=frozenset({"lifecycle"}),
        host=ce.GatewayHostOperations(),
    )
    with pytest.raises(ce.CapabilityDenied):
        facade.spawn_lifecycle_task("watcher", lambda: None)


def test_spawn_records_full_generation_identity():
    recorded: list[ce.GatewayBackgroundTask] = []
    facade = ce.GatewayRuntimeFacade(
        extension_id="probe",
        profile_name="a",
        profile_home=SCOPE_A,
        generation=9,
        capabilities=frozenset({"lifecycle"}),
        host=ce.GatewayHostOperations(spawn_task=recorded.append),
    )
    facade.spawn_lifecycle_task("watcher", lambda: None)
    assert recorded[0].identity == ("probe", SCOPE_A, "watcher", 9)


# ---------------------------------------------------------------------------
# production integration: the runtime sequence
# ---------------------------------------------------------------------------


class _Source:
    def __init__(self, profile="root", chat_type="dm"):
        self.platform = type("_P", (), {"value": "telegram"})()
        self.user_id = "+15551234"
        self.chat_id = "c1"
        self.chat_type = chat_type
        self.profile = profile


class _Event:
    def __init__(self, text="hello", profile="root"):
        self.text = text
        self.source = _Source(profile=profile)
        self.message_id = "m-1"


def test_build_route_context_captures_immutable_transport_identity():
    context = ce_runtime.build_route_context(
        _Event(), transport_profile="root", transport_home="/home/root"
    )
    assert context is not None
    assert context.transport_profile == "root"
    assert context.transport_home == "/home/root"
    with pytest.raises(Exception):
        context.transport_profile = "guest"  # type: ignore[misc]


def test_runtime_admission_denies_and_is_reported():
    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"admission_policy"}),
            authorize_route=lambda ctx: ce.GatewayRouteDirective(
                admit=False, reason="blocked"
            ),
        ),
        scope=SCOPE_A,
    )
    context = ce_runtime.build_route_context(
        _Event(), transport_profile="root", transport_home=SCOPE_A
    )
    decision = ce_runtime.admit_and_route(
        context, scope=SCOPE_A, served_profiles=("root",)
    )
    assert decision is not None
    assert decision.admitted is False
    assert decision.reason == "blocked"


def test_runtime_turn_policy_scope_binds_and_enforces():
    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"tool_authorization"}),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(
                request.function_name != "terminal", "denied"
            ),
        ),
        scope=SCOPE_A,
    )
    with ce_runtime.turn_policy_scope(scope=SCOPE_A, route_id="m-1") as policy:
        assert policy is not None
        assert ce.authorize_tool_dispatch("fs", {}) is None
        assert ce.authorize_tool_dispatch("terminal", {}) is not None
    assert ce.current_request_policy() is None


def test_runtime_turn_policy_scope_is_noop_without_extension():
    with ce_runtime.turn_policy_scope(scope=SCOPE_A, route_id="m-1") as policy:
        assert policy is None
        assert ce.authorize_tool_dispatch("terminal", {}) is None


def test_bound_policy_survives_the_real_executor_thread_hop():
    """Prove the production binding reaches tool-executor worker threads."""
    from tools.thread_context import propagate_context_to_thread

    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"tool_authorization"}),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(
                False, "denied in worker"
            ),
        ),
        scope=SCOPE_A,
    )
    results: list[object] = []

    def _work():
        results.append(ce.authorize_tool_dispatch("terminal", {"command": "ls"}))

    with ce_runtime.turn_policy_scope(scope=SCOPE_A, route_id="m-1"):
        thread = threading.Thread(target=propagate_context_to_thread(_work))
        thread.start()
        thread.join()

    assert results[0] is not None
    assert "denied in worker" in results[0]


def test_post_turn_observation_reaches_the_extension():
    seen: list[ce.GatewayTurnResult] = []
    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"post_turn_observer"}),
            observe_turn_result=seen.append,
        ),
        scope=SCOPE_A,
    )
    ce_runtime.observe_turn_completion(
        scope=SCOPE_A,
        session_key="s1",
        runtime_profile="root",
        platform="telegram",
        sender_identity="+15551234",
        user_text="hi",
        assistant_text="hello",
        delivered=True,
    )
    assert len(seen) == 1
    assert seen[0].assistant_text == "hello"


def test_ingress_observation_reaches_the_extension():
    seen: list[ce.GatewayRouteContext] = []
    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"ingress_observer"}),
            observe_ingress=seen.append,
        ),
        scope=SCOPE_A,
    )
    context = ce_runtime.build_route_context(
        _Event(), transport_profile="root", transport_home=SCOPE_A
    )
    ce_runtime.observe_authenticated_ingress(context, scope=SCOPE_A)
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# admission gate: required extensions block serving, not just /ready
# ---------------------------------------------------------------------------


def test_no_requirements_profile_may_serve():
    ok, reason = ce_runtime.profile_requirements_satisfied(
        scope=SCOPE_A, config_raw={"gateway": {}}
    )
    assert ok is True
    assert reason == ""


def test_missing_required_extension_blocks_serving():
    ok, reason = ce_runtime.profile_requirements_satisfied(
        scope=SCOPE_A,
        config_raw={
            "gateway": {"required_conversation_extensions": [{"id": "probe", "api_version": 1}]}
        },
    )
    assert ok is False
    assert "missing" in reason


def test_present_required_extension_allows_serving():
    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"tool_authorization"}),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
        ),
        scope=SCOPE_A,
    )
    ok, _ = ce_runtime.profile_requirements_satisfied(
        scope=SCOPE_A,
        config_raw={
            "gateway": {"required_conversation_extensions": [{"id": "probe", "api_version": 1}]}
        },
    )
    assert ok is True


def test_malformed_requirement_blocks_serving():
    ok, reason = ce_runtime.profile_requirements_satisfied(
        scope=SCOPE_A,
        config_raw={"gateway": {"required_conversation_extensions": [{"api_version": 1}]}},
    )
    assert ok is False
    assert reason == "malformed_requirement_declaration"


def test_unhealthy_required_extension_blocks_serving():
    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"tool_authorization", "health"}),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
            health=lambda: ce.GatewayExtensionHealth(healthy=False, detail="down"),
        ),
        scope=SCOPE_A,
    )
    ok, reason = ce_runtime.profile_requirements_satisfied(
        scope=SCOPE_A,
        config_raw={
            "gateway": {"required_conversation_extensions": [{"id": "probe", "api_version": 1}]}
        },
    )
    assert ok is False
    assert "unhealthy" in reason


def test_requirement_gate_is_profile_isolated():
    """Profile A satisfying the requirement must not unblock profile B."""
    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"tool_authorization"}),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
        ),
        scope=SCOPE_A,
    )
    config = {
        "gateway": {"required_conversation_extensions": [{"id": "probe", "api_version": 1}]}
    }
    assert ce_runtime.profile_requirements_satisfied(scope=SCOPE_A, config_raw=config)[0] is True
    assert ce_runtime.profile_requirements_satisfied(scope=SCOPE_B, config_raw=config)[0] is False


def test_missing_capability_blocks_serving():
    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"tool_authorization"}),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
        ),
        scope=SCOPE_A,
    )
    ok, reason = ce_runtime.profile_requirements_satisfied(
        scope=SCOPE_A,
        config_raw={
            "gateway": {
                "required_conversation_extensions": [
                    {"id": "probe", "api_version": 1, "capabilities": ["admission_policy"]}
                ]
            }
        },
    )
    assert ok is False
    assert "missing_capability" in reason
