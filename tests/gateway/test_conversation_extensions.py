"""Contract tests for the generic gateway conversation-extension seam.

These assert the *core* contract only: no Poke/Guest policy, no plugin
package import. Everything here must hold for any third-party extension.
"""

from __future__ import annotations

import threading

import pytest

from gateway import conversation_extensions as ce


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _bundle(
    extension_id: str = "ext",
    *,
    capabilities=("tool_authorization",),
    **kwargs,
) -> ce.GatewayConversationExtension:
    return ce.GatewayConversationExtension(
        extension_id=extension_id,
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset(capabilities),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    yield
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()


# --------------------------------------------------------------------------
# bundle validation
# --------------------------------------------------------------------------


def test_bundle_rejects_unknown_capability():
    with pytest.raises(ValueError):
        _bundle(capabilities=("not_a_real_capability",))


def test_bundle_rejects_incompatible_api_version():
    with pytest.raises(ValueError):
        ce.GatewayConversationExtension(
            extension_id="ext",
            api_version=ce.EXTENSION_API_VERSION + 1,
            capabilities=frozenset({"tool_authorization"}),
        )


def test_bundle_requires_callable_for_declared_capability():
    """Declaring a capability without supplying its callback is a contract error."""
    with pytest.raises(ValueError):
        _bundle(capabilities=("admission_policy",))  # no authorize_route given


def test_bundle_rejects_callback_without_declared_capability():
    with pytest.raises(ValueError):
        _bundle(
            capabilities=("tool_authorization",),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
            authorize_route=lambda ctx: ce.GatewayRouteDirective(admit=True),
        )


def test_bundle_is_immutable():
    bundle = _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True))
    with pytest.raises(Exception):
        bundle.extension_id = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------
# registration / unload / generations
# --------------------------------------------------------------------------


def test_register_and_lookup_is_profile_scoped():
    bundle = _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True))
    reg = ce.conversation_extension_registry
    reg.register(bundle, scope="/home/a")

    assert reg.get("ext", scope="/home/a") is bundle
    assert reg.get("ext", scope="/home/b") is None
    assert reg.active_ids(scope="/home/b") == ()


def test_unload_removes_only_its_own_generation():
    reg = ce.conversation_extension_registry
    first = _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True))
    second = _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(False, "no"))

    gen1 = reg.register(first, scope="/home/a")
    gen2 = reg.register(second, scope="/home/a")
    assert reg.get("ext", scope="/home/a") is second

    # Stale generation unload must NOT clear the newer registration.
    assert reg.unregister("ext", generation=gen1, scope="/home/a") is False
    assert reg.get("ext", scope="/home/a") is second

    assert reg.unregister("ext", generation=gen2, scope="/home/a") is True
    assert reg.get("ext", scope="/home/a") is None


def test_generations_are_monotonic_and_unique_across_scopes():
    reg = ce.conversation_extension_registry
    bundle = _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True))
    seen = {
        reg.register(bundle, scope="/home/a"),
        reg.register(bundle, scope="/home/b"),
        reg.register(bundle, scope="/home/a"),
    }
    assert len(seen) == 3


def test_registration_is_atomic_under_concurrency():
    reg = ce.conversation_extension_registry
    bundle = _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True))
    generations: list[int] = []
    barrier = threading.Barrier(8)

    def _worker():
        barrier.wait()
        generations.append(reg.register(bundle, scope="/home/a"))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(generations)) == 8
    # Exactly one generation survives as active.
    assert reg.get("ext", scope="/home/a") is bundle
    assert reg.active_generation("ext", scope="/home/a") == max(generations)


# --------------------------------------------------------------------------
# requirements / readiness (core-owned, fail closed)
# --------------------------------------------------------------------------


def test_requirement_parsing_rejects_malformed_entries():
    with pytest.raises(ValueError):
        ce.parse_required_extensions([{"api_version": 1}])  # missing id
    with pytest.raises(ValueError):
        ce.parse_required_extensions([{"id": "x", "api_version": 1, "capabilities": ["bogus"]}])


def test_readiness_ok_when_no_requirements_and_no_extension():
    report = ce.evaluate_extension_readiness(required=(), scope="/home/a")
    assert report.ready is True
    assert report.status == "ok"


def test_readiness_fails_closed_when_required_extension_missing():
    required = ce.parse_required_extensions([{"id": "ext", "api_version": 1}])
    report = ce.evaluate_extension_readiness(required=required, scope="/home/a")
    assert report.ready is False
    assert report.status == "unready"
    assert any(item.reason == "missing" for item in report.checks)


def test_readiness_fails_closed_on_missing_capability():
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True)),
        scope="/home/a",
    )
    required = ce.parse_required_extensions(
        [{"id": "ext", "api_version": 1, "capabilities": ["admission_policy"]}]
    )
    report = ce.evaluate_extension_readiness(required=required, scope="/home/a")
    assert report.ready is False
    assert any(item.reason == "missing_capability" for item in report.checks)


def test_readiness_fails_closed_on_unhealthy_extension():
    def _health():
        return ce.GatewayExtensionHealth(healthy=False, detail="degraded")

    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(
            capabilities=("tool_authorization", "health"),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
            health=_health,
        ),
        scope="/home/a",
    )
    required = ce.parse_required_extensions([{"id": "ext", "api_version": 1}])
    report = ce.evaluate_extension_readiness(required=required, scope="/home/a")
    assert report.ready is False
    assert any(item.reason == "unhealthy" for item in report.checks)


def test_readiness_fails_closed_when_health_probe_raises():
    def _health():
        raise RuntimeError("boom")

    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(
            capabilities=("tool_authorization", "health"),
            authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
            health=_health,
        ),
        scope="/home/a",
    )
    required = ce.parse_required_extensions([{"id": "ext", "api_version": 1}])
    report = ce.evaluate_extension_readiness(required=required, scope="/home/a")
    assert report.ready is False
    # Never leaks the exception message into the report.
    assert all("boom" not in (item.detail or "") for item in report.checks)


def test_readiness_ignores_extensions_in_other_profiles():
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True)),
        scope="/home/other",
    )
    required = ce.parse_required_extensions([{"id": "ext", "api_version": 1}])
    report = ce.evaluate_extension_readiness(required=required, scope="/home/a")
    assert report.ready is False


# --------------------------------------------------------------------------
# route / admission sequence
# --------------------------------------------------------------------------


def _route_ctx(**kwargs) -> ce.GatewayRouteContext:
    base = dict(
        platform="telegram",
        adapter_identity="tg:bot",
        transport_profile="root",
        transport_home="/home/root",
        sender_identity="+1555",
        chat_id="c1",
        chat_type="dm",
    )
    base.update(kwargs)
    return ce.GatewayRouteContext(**base)


def test_route_without_extension_admits_transport_profile_unchanged():
    decision = ce.resolve_route(
        _route_ctx(), scope="/home/root", served_profiles=("root",), permitted_routes={}
    )
    assert decision.admitted is True
    assert decision.runtime_profile == "root"
    assert decision.transport_profile == "root"
    assert decision.extension_id is None


def test_route_directive_cannot_reach_unserved_profile():
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(
            capabilities=("admission_policy",),
            authorize_route=lambda ctx: ce.GatewayRouteDirective(admit=True, runtime_profile="ghost"),
        ),
        scope="/home/root",
    )
    decision = ce.resolve_route(
        _route_ctx(), scope="/home/root", served_profiles=("root",), permitted_routes={"root": ("guest",)}
    )
    assert decision.admitted is False
    assert decision.reason == "route_not_served"


def test_route_directive_cannot_reach_unpermitted_profile():
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(
            capabilities=("admission_policy",),
            authorize_route=lambda ctx: ce.GatewayRouteDirective(admit=True, runtime_profile="admin"),
        ),
        scope="/home/root",
    )
    decision = ce.resolve_route(
        _route_ctx(),
        scope="/home/root",
        served_profiles=("root", "admin"),
        permitted_routes={"root": ("guest",)},
    )
    assert decision.admitted is False
    assert decision.reason == "route_not_permitted"


def test_route_directive_reaches_permitted_profile_but_transport_home_is_immutable():
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(
            capabilities=("admission_policy",),
            authorize_route=lambda ctx: ce.GatewayRouteDirective(admit=True, runtime_profile="guest"),
        ),
        scope="/home/root",
    )
    decision = ce.resolve_route(
        _route_ctx(),
        scope="/home/root",
        served_profiles=("root", "guest"),
        permitted_routes={"root": ("guest",)},
    )
    assert decision.admitted is True
    assert decision.runtime_profile == "guest"
    # Transport trust domain never moves with the runtime route.
    assert decision.transport_profile == "root"
    assert decision.transport_home == "/home/root"
    assert decision.extension_id == "ext"


def test_route_denies_closed_when_extension_raises():
    def _boom(ctx):
        raise RuntimeError("policy exploded")

    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(capabilities=("admission_policy",), authorize_route=_boom),
        scope="/home/root",
    )
    decision = ce.resolve_route(
        _route_ctx(), scope="/home/root", served_profiles=("root",), permitted_routes={}
    )
    assert decision.admitted is False
    assert decision.reason == "extension_error"


def test_route_denies_closed_on_malformed_directive():
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(capabilities=("admission_policy",), authorize_route=lambda ctx: {"admit": True}),
        scope="/home/root",
    )
    decision = ce.resolve_route(
        _route_ctx(), scope="/home/root", served_profiles=("root",), permitted_routes={}
    )
    assert decision.admitted is False
    assert decision.reason == "malformed_directive"


def test_route_context_is_immutable():
    ctx = _route_ctx()
    with pytest.raises(Exception):
        ctx.transport_profile = "guest"  # type: ignore[misc]


# --------------------------------------------------------------------------
# request policy token + final-dispatch tool authorization
# --------------------------------------------------------------------------


def test_no_policy_and_no_extension_allows_tools():
    assert ce.authorize_tool_dispatch("fs", {"path": "/tmp/x"}) is None


def test_policy_scope_enforces_extension_decision():
    reg = ce.conversation_extension_registry
    calls: list[str] = []

    def _authorize(request: ce.GatewayToolAuthorizationRequest):
        calls.append(request.function_name)
        if request.function_name == "terminal":
            return ce.GatewayToolAuthorizationDecision(False, "terminal denied")
        return ce.GatewayToolAuthorizationDecision(True)

    reg.register(_bundle(authorize_tool=_authorize), scope="/home/a")

    with ce.request_policy_scope(
        ce.issue_request_policy(extension_id="ext", profile_home="/home/a", route_id="r1")
    ):
        assert ce.authorize_tool_dispatch("fs", {}) is None
        denial = ce.authorize_tool_dispatch("terminal", {"command": "ls"})
        assert denial is not None
        assert "terminal denied" in denial

    assert calls == ["fs", "terminal"]


def test_policy_scope_fails_closed_when_required_extension_vanished():
    """A live policy token whose extension unloaded mid-turn denies, never allows."""
    reg = ce.conversation_extension_registry
    gen = reg.register(
        _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True)),
        scope="/home/a",
    )
    policy = ce.issue_request_policy(
        extension_id="ext", profile_home="/home/a", route_id="r1"
    )
    reg.unregister("ext", generation=gen, scope="/home/a")

    with ce.request_policy_scope(policy):
        denial = ce.authorize_tool_dispatch("fs", {})
    assert denial is not None
    assert "policy_unavailable" in denial


def test_policy_scope_fails_closed_when_extension_raises():
    def _boom(request):
        raise RuntimeError("authz exploded")

    reg = ce.conversation_extension_registry
    reg.register(_bundle(authorize_tool=_boom), scope="/home/a")

    with ce.request_policy_scope(
        ce.issue_request_policy(extension_id="ext", profile_home="/home/a", route_id="r1")
    ):
        denial = ce.authorize_tool_dispatch("terminal", {"command": "ls"})
    assert denial is not None
    assert "authz exploded" not in denial


def test_policy_scope_fails_closed_on_malformed_decision():
    reg = ce.conversation_extension_registry
    reg.register(_bundle(authorize_tool=lambda request: "yes please"), scope="/home/a")

    with ce.request_policy_scope(
        ce.issue_request_policy(extension_id="ext", profile_home="/home/a", route_id="r1")
    ):
        assert ce.authorize_tool_dispatch("fs", {}) is not None


def test_policy_token_is_immutable_and_scoped():
    policy = ce.issue_request_policy(
        extension_id="ext", profile_home="/home/a", route_id="r1"
    )
    with pytest.raises(Exception):
        policy.profile_home = "/home/b"  # type: ignore[misc]

    assert ce.current_request_policy() is None
    with ce.request_policy_scope(policy):
        assert ce.current_request_policy() is policy
    assert ce.current_request_policy() is None


def test_policy_does_not_leak_across_threads_without_propagation():
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(False, "deny")),
        scope="/home/a",
    )
    seen: list[object] = []

    def _worker():
        seen.append(ce.current_request_policy())

    with ce.request_policy_scope(
        ce.issue_request_policy(extension_id="ext", profile_home="/home/a", route_id="r1")
    ):
        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join()

    assert seen == [None]


def test_policy_propagates_across_thread_with_context_copy():
    """The real executor hop uses contextvars.copy_context, which must carry policy."""
    import contextvars

    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(False, "deny")),
        scope="/home/a",
    )
    results: list[object] = []

    def _worker():
        results.append(ce.authorize_tool_dispatch("terminal", {"command": "ls"}))

    with ce.request_policy_scope(
        ce.issue_request_policy(extension_id="ext", profile_home="/home/a", route_id="r1")
    ):
        ctx = contextvars.copy_context()
        thread = threading.Thread(target=ctx.run, args=(_worker,))
        thread.start()
        thread.join()

    assert results[0] is not None
    assert "deny" in results[0]


def test_profile_isolation_of_policy_decisions():
    """An extension in profile A must never authorize a turn routed to profile B."""
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True)),
        scope="/home/a",
    )
    with ce.request_policy_scope(
        ce.issue_request_policy(extension_id="ext", profile_home="/home/b", route_id="r1")
    ):
        # No extension registered under /home/b -> fail closed, not "allow via A".
        assert ce.authorize_tool_dispatch("fs", {}) is not None


# --------------------------------------------------------------------------
# runtime facade: bounded capability surface
# --------------------------------------------------------------------------


def test_facade_denies_undeclared_capabilities():
    bundle = _bundle(authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True))
    facade = ce.GatewayRuntimeFacade(
        extension_id="ext",
        profile_name="root",
        profile_home="/home/a",
        generation=1,
        capabilities=bundle.capabilities,
        host=ce.GatewayHostOperations(),
    )
    with pytest.raises(ce.CapabilityDenied):
        facade.spawn_lifecycle_task("k", lambda: None)
    with pytest.raises(ce.CapabilityDenied):
        facade.create_initiated_child(
            ce.InitiatedTurnRequest(session_key="s", prompt="hi", origin="test")
        )
    with pytest.raises(ce.CapabilityDenied):
        facade.inject_turn(session_key="s", text="hi")


def test_facade_never_exposes_runner_or_stores():
    facade = ce.GatewayRuntimeFacade(
        extension_id="ext",
        profile_name="root",
        profile_home="/home/a",
        generation=1,
        capabilities=frozenset({"tool_authorization"}),
        host=ce.GatewayHostOperations(),
    )
    public = {name for name in dir(facade) if not name.startswith("_")}
    forbidden = {"runner", "gateway", "session_store", "adapter", "client", "credentials", "config"}
    assert not (public & forbidden)


def test_facade_lifecycle_task_identity_includes_generation():
    started: list[ce.GatewayBackgroundTask] = []
    host = ce.GatewayHostOperations(spawn_task=lambda task: started.append(task))
    facade = ce.GatewayRuntimeFacade(
        extension_id="ext",
        profile_name="root",
        profile_home="/home/a",
        generation=7,
        capabilities=frozenset({"lifecycle"}),
        host=host,
    )
    facade.spawn_lifecycle_task("watcher", lambda: None)
    assert started[0].identity == ("ext", "/home/a", "watcher", 7)


def test_facade_health_report_is_bounded():
    facade = ce.GatewayRuntimeFacade(
        extension_id="ext",
        profile_name="root",
        profile_home="/home/a",
        generation=1,
        capabilities=frozenset({"health"}),
        host=ce.GatewayHostOperations(),
    )
    snapshot = facade.describe()
    assert snapshot == {
        "extension_id": "ext",
        "profile": "root",
        "generation": 1,
        "capabilities": ["health"],
    }


# --------------------------------------------------------------------------
# turn augmentation: optional enrichment fails open
# --------------------------------------------------------------------------


def _turn_ctx() -> ce.GatewayTurnContext:
    return ce.GatewayTurnContext(
        session_key="s1",
        runtime_profile="root",
        platform="telegram",
        sender_identity="+1555",
        chat_type="dm",
        user_text="hello",
    )


def test_turn_augmentation_absent_without_extension():
    aug = ce.collect_turn_augmentation(_turn_ctx(), scope="/home/a")
    assert aug.user_context == ()
    assert aug.system_context == ()
    assert aug.request_tools == ()


def test_turn_augmentation_fails_open_on_exception():
    def _boom(ctx):
        raise RuntimeError("enrichment exploded")

    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(capabilities=("turn_policy",), augment_turn=_boom), scope="/home/a"
    )
    aug = ce.collect_turn_augmentation(_turn_ctx(), scope="/home/a")
    assert aug.user_context == ()
    assert aug.degraded is True


def test_turn_augmentation_fails_open_on_malformed_result():
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(capabilities=("turn_policy",), augment_turn=lambda ctx: ["nope"]),
        scope="/home/a",
    )
    aug = ce.collect_turn_augmentation(_turn_ctx(), scope="/home/a")
    assert aug.degraded is True


def test_turn_augmentation_returns_extension_context():
    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(
            capabilities=("turn_policy",),
            augment_turn=lambda ctx: ce.GatewayTurnAugmentation(
                user_context=("recalled fact",), system_context=("be brief",)
            ),
        ),
        scope="/home/a",
    )
    aug = ce.collect_turn_augmentation(_turn_ctx(), scope="/home/a")
    assert aug.user_context == ("recalled fact",)
    assert aug.system_context == ("be brief",)
    assert aug.degraded is False


# --------------------------------------------------------------------------
# observation fire sites never raise into the gateway
# --------------------------------------------------------------------------


def test_observation_swallows_extension_errors():
    def _boom(*args, **kwargs):
        raise RuntimeError("observer exploded")

    reg = ce.conversation_extension_registry
    reg.register(
        _bundle(
            capabilities=("ingress_observer", "post_turn_observer"),
            observe_ingress=_boom,
            observe_turn_result=_boom,
        ),
        scope="/home/a",
    )
    # Must not raise.
    ce.notify_ingress(_route_ctx(), scope="/home/a")
    ce.notify_turn_result(
        ce.GatewayTurnResult(
            session_key="s1",
            runtime_profile="root",
            platform="telegram",
            sender_identity="+1555",
            user_text="hi",
            assistant_text="hello",
            delivered=True,
        ),
        scope="/home/a",
    )


def test_turn_result_is_immutable():
    result = ce.GatewayTurnResult(
        session_key="s1",
        runtime_profile="root",
        platform="telegram",
        sender_identity="+1555",
        user_text="hi",
        assistant_text="hello",
        delivered=True,
    )
    with pytest.raises(Exception):
        result.delivered = False  # type: ignore[misc]


# --------------------------------------------------------------------------
# authenticated existing-DM send contract (tri-state, no create, no fallback)
# --------------------------------------------------------------------------


def test_dm_send_result_tri_state_values():
    assert ce.DmSendOutcome.SENT.value == "sent"
    assert ce.DmSendOutcome.DEFINITIVE_FAILURE.value == "definitive_failure"
    assert ce.DmSendOutcome.UNKNOWN.value == "unknown"


def test_dm_send_requires_reservation_key():
    with pytest.raises(ValueError):
        ce.AuthenticatedDmRequest(
            platform="telegram", chat_id="c1", text="hi", reservation_key=""
        )


def test_dm_send_request_is_immutable_and_cannot_create_chats():
    request = ce.AuthenticatedDmRequest(
        platform="telegram", chat_id="c1", text="hi", reservation_key="r1"
    )
    assert not hasattr(request, "create_if_missing")
    with pytest.raises(Exception):
        request.chat_id = "c2"  # type: ignore[misc]
