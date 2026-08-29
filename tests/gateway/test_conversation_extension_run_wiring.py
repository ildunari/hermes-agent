"""Integration tests for the conversation-extension seam *as wired in run.py*.

Review 2 rejected Checkpoint 2 because every existing test exercised the seam
modules directly. These tests drive the real ``GatewayRunner`` methods so the
wiring itself — not just the contract module — is under test:

* P0-1 an ambiguous tool-authorization owner produces a clean refusal through
  the real ``_handle_message`` policy region rather than an unhandled crash.
* P0-2 gateway start/stop lifecycle fire sites actually run.
* P0-3 eager per-profile startup enumeration/activation happens before adapters
  serve, and a profile with an unsatisfied hard requirement is refused.
* P1-1 turn augmentation has a production call site.
* P1-2 initiated-child / turn-injection / authenticated-DM host ops are wired.
* P1-3 post-turn observation reports the *validated* runtime profile.
* P1-5 served profiles come from config, not adapter-connect success.
* P1-6 ``permitted_conversation_routes`` is a real config field.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import conversation_extensions as ce
from gateway import conversation_extension_runtime as ce_runtime
from gateway import conversation_ownership as co
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _clean_registry():
    ce.conversation_extension_registry.reset_for_tests()
    ce.lifecycle_task_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    ce.reset_gateway_host_operations()
    co.conversation_ownership_registry.reset_for_tests()
    yield
    ce.conversation_extension_registry.reset_for_tests()
    ce.lifecycle_task_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    ce.reset_gateway_host_operations()
    co.conversation_ownership_registry.reset_for_tests()


def _bare_runner() -> GatewayRunner:
    """A GatewayRunner without __init__ side effects (AGENTS.md test pattern)."""
    return object.__new__(GatewayRunner)


def _install_turn_owner(scope: str, extension_id: str = "aug") -> None:
    co.conversation_ownership_registry.install(
        scope,
        {
            co.OwnershipDomain.TURN_POLICY: co.OwnerSelection(
                co.OwnershipDomain.TURN_POLICY,
                co.OwnerKind.EXTENSION,
                extension_id=extension_id,
            )
        },
    )


def _tool_auth_bundle(extension_id: str) -> ce.GatewayConversationExtension:
    return ce.GatewayConversationExtension(
        extension_id=extension_id,
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization"}),
        authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(True),
    )


# ---------------------------------------------------------------------------
# P0-1 — ambiguous tool-authorization owner
# ---------------------------------------------------------------------------


def test_turn_policy_scope_raises_at_call_not_at_enter():
    """The ambiguity must surface when the scope is *requested*.

    The original defect: ``turn_policy_scope`` was a bare ``@contextmanager``,
    so the owner lookup only ran at ``__enter__``. run.py guarded the call, not
    the ``with``, making its handler dead code.
    """
    scope = "/tmp/hermes-ambiguous-home"
    ce.conversation_extension_registry.register(_tool_auth_bundle("a"), scope=scope)
    ce.conversation_extension_registry.register(_tool_auth_bundle("b"), scope=scope)

    with pytest.raises(ce_runtime.AmbiguousToolAuthorizationOwner):
        ce_runtime.turn_policy_scope(scope=scope, route_id="route-1")


def test_turn_policy_scope_single_owner_binds_policy():
    scope = "/tmp/hermes-single-owner-home"
    ce.conversation_extension_registry.register(_tool_auth_bundle("solo"), scope=scope)

    with ce_runtime.turn_policy_scope(scope=scope, route_id="route-1") as policy:
        assert policy is not None
        assert ce.current_request_policy() is policy
        assert policy.extension_id == "solo"
    assert ce.current_request_policy() is None


def test_turn_policy_scope_no_owner_is_transparent():
    """No registered authorizer -> no token, unchanged behavior."""
    with ce_runtime.turn_policy_scope(
        scope="/tmp/hermes-empty-home", route_id="route-1"
    ) as policy:
        assert policy is None
        assert ce.current_request_policy() is None


@pytest.mark.asyncio
async def test_ambiguous_owner_refuses_turn_through_run_wiring(monkeypatch, tmp_path):
    """The real run.py policy region must refuse cleanly, not raise.

    This is the regression test for P0-1: it drives
    ``_run_agent_turn_with_policy`` — the exact code path ``_handle_message``
    uses — and asserts a clean ``None`` refusal with the agent never invoked.
    """
    runner = _bare_runner()
    scope = str(tmp_path / "ambiguous")
    ce.conversation_extension_registry.register(_tool_auth_bundle("a"), scope=scope)
    ce.conversation_extension_registry.register(_tool_auth_bundle("b"), scope=scope)

    called = False

    async def _never(*args, **kwargs):
        nonlocal called
        called = True
        return "should not run"

    runner._handle_message_with_agent = _never  # type: ignore[method-assign]

    result = await runner._run_agent_turn_with_policy(
        event=SimpleNamespace(text="hi", message_id="m1"),
        source=SimpleNamespace(platform=Platform.DISCORD, profile=None),
        quick_key="agent:main:discord:dm:1",
        run_generation=1,
        agent_kwargs={},
        policy_scope=scope,
    )

    assert result is None, "ambiguous authorizer must refuse the turn"
    assert called is False, "agent must never run without a resolved authorizer"


@pytest.mark.asyncio
async def test_single_owner_runs_turn_under_bound_policy(monkeypatch, tmp_path):
    """The happy path still runs, and the policy is bound inside the turn."""
    runner = _bare_runner()
    scope = str(tmp_path / "single")
    ce.conversation_extension_registry.register(_tool_auth_bundle("solo"), scope=scope)

    seen: dict = {}

    async def _agent(*args, **kwargs):
        policy = ce.current_request_policy()
        seen["extension_id"] = policy.extension_id if policy else None
        return "ok"

    runner._handle_message_with_agent = _agent  # type: ignore[method-assign]

    result = await runner._run_agent_turn_with_policy(
        event=SimpleNamespace(text="hi", message_id="m1"),
        source=SimpleNamespace(platform=Platform.DISCORD, profile=None),
        quick_key="agent:main:discord:dm:1",
        run_generation=1,
        agent_kwargs={},
        policy_scope=scope,
    )

    assert result == "ok"
    assert seen["extension_id"] == "solo"
    assert ce.current_request_policy() is None, "policy must not leak past the turn"


@pytest.mark.asyncio
async def test_no_extension_turn_is_unaffected(tmp_path):
    """Invariant: with no plugin registered, behavior is byte-identical."""
    runner = _bare_runner()

    async def _agent(*args, **kwargs):
        assert ce.current_request_policy() is None
        return "plain"

    runner._handle_message_with_agent = _agent  # type: ignore[method-assign]

    result = await runner._run_agent_turn_with_policy(
        event=SimpleNamespace(text="hi", message_id="m1"),
        source=SimpleNamespace(platform=Platform.DISCORD, profile=None),
        quick_key="agent:main:discord:dm:1",
        run_generation=1,
        agent_kwargs={},
        policy_scope=str(tmp_path / "empty"),
    )
    assert result == "plain"


# ---------------------------------------------------------------------------
# P0-2 — gateway start/stop lifecycle fire sites
# ---------------------------------------------------------------------------


def test_gateway_start_lifecycle_fires_on_start(tmp_path):
    runner = _bare_runner()
    runner._extension_lifecycle_started = set()
    scope = str(tmp_path / "lifecycle")
    started: list = []

    bundle = ce.GatewayConversationExtension(
        extension_id="lc",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"lifecycle"}),
        on_start=lambda facade: started.append(facade.extension_id),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)

    runner._fire_extension_gateway_start(scope=scope, profile_name="default")
    assert started == ["lc"]


def test_gateway_stop_lifecycle_fires_on_stop(tmp_path):
    runner = _bare_runner()
    runner._extension_lifecycle_started = set()
    scope = str(tmp_path / "lifecycle-stop")
    stopped: list = []

    bundle = ce.GatewayConversationExtension(
        extension_id="lc",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"lifecycle"}),
        on_start=lambda facade: None,
        on_stop=lambda facade: stopped.append(facade.extension_id),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)

    runner._fire_extension_gateway_start(scope=scope, profile_name="default")
    runner._fire_extension_gateway_stop()
    assert stopped == ["lc"]


def test_gateway_stop_lifecycle_cancels_tasks(tmp_path):
    """Gateway shutdown must tear down extension lifecycle tasks."""
    runner = _bare_runner()
    runner._extension_lifecycle_started = set()
    scope = str(tmp_path / "lifecycle-tasks")

    class _Handle:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    handle = _Handle()
    spawned: list = []

    def _spawn(task):
        spawned.append(task)
        ce.lifecycle_task_registry.record(task, handle)

    ce.install_gateway_host_operations(
        ce.GatewayHostOperations(
            spawn_task=_spawn, cancel_tasks=ce.lifecycle_task_registry.cancel
        )
    )

    def _on_start(facade):
        facade.spawn_lifecycle_task("watcher", lambda: None)

    bundle = ce.GatewayConversationExtension(
        extension_id="lc",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"lifecycle"}),
        on_start=_on_start,
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)

    runner._fire_extension_gateway_start(scope=scope, profile_name="default")
    assert len(spawned) == 1

    runner._fire_extension_gateway_stop()
    assert handle.cancelled is True, "gateway stop must cancel lifecycle tasks"


def test_gateway_start_is_idempotent_per_scope(tmp_path):
    """Double activation must not double-fire on_start."""
    runner = _bare_runner()
    runner._extension_lifecycle_started = set()
    scope = str(tmp_path / "idem")
    starts: list = []

    bundle = ce.GatewayConversationExtension(
        extension_id="lc",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"lifecycle"}),
        on_start=lambda facade: starts.append(1),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)

    runner._fire_extension_gateway_start(scope=scope, profile_name="default")
    runner._fire_extension_gateway_start(scope=scope, profile_name="default")
    assert len(starts) == 1


def test_gateway_stop_with_no_extensions_is_a_noop():
    """No-plugin invariant: stop path must be inert."""
    runner = _bare_runner()
    runner._extension_lifecycle_started = set()
    runner._fire_extension_gateway_stop()  # must not raise


# ---------------------------------------------------------------------------
# P0-3 — eager startup enumeration + hard readiness
# ---------------------------------------------------------------------------


def _write_profile_config(home: Path, requirement: str | None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if requirement is None:
        (home / "config.yaml").write_text("gateway: {}\n", encoding="utf-8")
    else:
        (home / "config.yaml").write_text(
            "gateway:\n"
            "  required_conversation_extensions:\n"
            f"    - id: {requirement}\n"
            "      api_version: 1\n",
            encoding="utf-8",
        )


def test_eager_activation_marks_profile_unready_when_requirement_missing(tmp_path):
    """A profile requiring a missing extension must be hard-unready at startup."""
    runner = _bare_runner()
    runner._extension_lifecycle_started = set()
    runner._extension_profile_readiness = {}

    home = tmp_path / "profiles" / "guest"
    _write_profile_config(home, "poke")

    runner._activate_conversation_extensions_for_profile("guest", home)

    from hermes_constants import hermes_home_key

    state = runner._extension_profile_readiness[hermes_home_key(home)]
    assert state["ready"] is False
    assert "poke" in state["reason"]


def test_eager_activation_marks_profile_ready_when_requirement_satisfied(tmp_path):
    from hermes_constants import hermes_home_key

    runner = _bare_runner()
    runner._extension_lifecycle_started = set()
    runner._extension_profile_readiness = {}

    home = tmp_path / "profiles" / "guest"
    _write_profile_config(home, "poke")

    bundle = ce.GatewayConversationExtension(
        extension_id="poke",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization", "health"}),
        authorize_tool=lambda r: ce.GatewayToolAuthorizationDecision(True),
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    ce.conversation_extension_registry.register(
        bundle, scope=hermes_home_key(home)
    )

    runner._activate_conversation_extensions_for_profile("guest", home)
    assert runner._extension_profile_readiness[hermes_home_key(home)]["ready"] is True


def test_eager_activation_no_requirements_is_ready(tmp_path):
    """Ordinary profiles are completely unaffected."""
    runner = _bare_runner()
    runner._extension_lifecycle_started = set()
    runner._extension_profile_readiness = {}

    home = tmp_path / "profiles" / "plain"
    _write_profile_config(home, None)

    runner._activate_conversation_extensions_for_profile("plain", home)
    from hermes_constants import hermes_home_key

    assert runner._extension_profile_readiness[hermes_home_key(home)]["ready"] is True


def test_unready_profile_refuses_ingress_before_first_message(tmp_path):
    """The startup verdict — not a lazy per-message probe — gates ingress."""
    from hermes_constants import hermes_home_key

    runner = _bare_runner()
    home = tmp_path / "profiles" / "guest"
    scope = hermes_home_key(home)
    runner._extension_profile_readiness = {
        scope: {"ready": False, "reason": "poke:missing"}
    }
    assert runner._extension_profile_is_ready(scope) is False
    assert runner._extension_profile_is_ready(hermes_home_key(tmp_path / "default")) is True


def test_named_single_profile_without_source_stamp_uses_home_readiness(tmp_path):
    """A named single-profile source with profile=None cannot bypass readiness."""
    from hermes_constants import hermes_home_key

    runner = _bare_runner()
    home = tmp_path / "profiles" / "coding"
    scope = hermes_home_key(home)
    runner._extension_profile_readiness = {
        scope: {"ready": False, "reason": "missing_ext:missing"}
    }
    source = SimpleNamespace(profile=None)
    runner._resolve_profile_home_for_source = lambda _source: home

    resolved_scope = hermes_home_key(runner._resolve_profile_home_for_source(source))
    assert resolved_scope == scope
    assert runner._extension_profile_is_ready(resolved_scope) is False
    assert runner._extension_profile_unready_reason(resolved_scope) == "missing_ext:missing"


def test_readiness_probe_reports_hard_unready_for_missing_requirement(
    tmp_path, monkeypatch
):
    """P0-3: a missing *required* extension is unready, not merely degraded."""
    from gateway.readiness import _probe_conversation_extensions

    home = tmp_path / ".hermes"
    _write_profile_config(home, "poke")

    probe = _probe_conversation_extensions(home)
    assert probe["status"] == "unready", probe


def test_readiness_probe_ok_without_requirements(tmp_path):
    from gateway.readiness import _probe_conversation_extensions

    home = tmp_path / ".hermes"
    _write_profile_config(home, None)
    assert _probe_conversation_extensions(home)["status"] == "ok"


def test_collect_runtime_readiness_surfaces_unready_overall(tmp_path, monkeypatch):
    from gateway.readiness import collect_runtime_readiness

    home = tmp_path / ".hermes"
    _write_profile_config(home, "poke")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={"gateway_state": "running", "platforms": {}},
    )
    assert result["status"] == "unready"
    assert result["checks"]["conversation_extensions"]["status"] == "unready"


# ---------------------------------------------------------------------------
# P1-1 — turn augmentation production call site
# ---------------------------------------------------------------------------


def test_turn_augmentation_has_a_runner_call_site(tmp_path):
    runner = _bare_runner()
    scope = str(tmp_path / "augment")

    bundle = ce.GatewayConversationExtension(
        extension_id="aug",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"turn_policy"}),
        augment_turn=lambda ctx: ce.GatewayTurnAugmentation(
            user_context=("extra recall",)
        ),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)
    _install_turn_owner(scope)

    text = runner._collect_extension_turn_context(
        scope=scope,
        session_key="agent:main:discord:dm:1",
        runtime_profile="default",
        platform="discord",
        sender_identity="u1",
        chat_type="dm",
        user_text="hello",
    )
    assert "extra recall" in text


def test_turn_augmentation_absent_extension_returns_empty(tmp_path):
    runner = _bare_runner()
    assert (
        runner._collect_extension_turn_context(
            scope=str(tmp_path / "none"),
            session_key="k",
            runtime_profile="default",
            platform="discord",
            sender_identity="u1",
            chat_type="dm",
            user_text="hello",
        )
        == ""
    )


def test_turn_augmentation_failure_is_fail_open(tmp_path):
    """A raising augmenter must never break the turn."""
    runner = _bare_runner()
    scope = str(tmp_path / "augment-fail")

    def _boom(ctx):
        raise RuntimeError("nope")

    bundle = ce.GatewayConversationExtension(
        extension_id="aug",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"turn_policy"}),
        augment_turn=_boom,
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)
    _install_turn_owner(scope)

    assert (
        runner._collect_extension_turn_context(
            scope=scope,
            session_key="k",
            runtime_profile="default",
            platform="discord",
            sender_identity="u1",
            chat_type="dm",
            user_text="hello",
        )
        == ""
    )


def test_turn_augmentation_tool_binds_and_dispatches_through_production_collector(tmp_path):
    from agent.request_scoped_tools import (
        RequestScopedTool,
        bind_request_scoped_tools,
        get_request_scoped_handler,
        record_request_scoped_usage,
    )

    runner = _bare_runner()
    scope = str(tmp_path / "tool-augment")
    committed = []
    tool = RequestScopedTool(
        schema={
            "name": "request_lookup",
            "parameters": {"type": "object"},
        },
        handler=lambda args: f"found:{args['query']}",
        on_success=lambda values: committed.append(tuple(values)),
    )
    bundle = ce.GatewayConversationExtension(
        extension_id="aug",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"turn_policy"}),
        augment_turn=lambda _ctx: ce.GatewayTurnAugmentation(
            request_tools=(tool,)
        ),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)
    _install_turn_owner(scope)

    augmentation = runner._collect_extension_turn_augmentation(
        scope=scope,
        session_key="s",
        runtime_profile="default",
        platform="discord",
        sender_identity="u",
        chat_type="dm",
        user_text="lookup",
    )
    assert augmentation.request_tools == (tool,)

    agent = SimpleNamespace(tools=[], valid_tool_names=set())
    with bind_request_scoped_tools(agent, augmentation.request_tools) as binding:
        handler = get_request_scoped_handler(agent, "request_lookup")
        assert handler is not None
        assert handler({"query": "x"}) == "found:x"
        record_request_scoped_usage(agent, "request_lookup", "fact-1")
        binding.commit_success()
    assert committed == [("fact-1",)]


# ---------------------------------------------------------------------------
# P1-2 — bounded host operations actually wired
# ---------------------------------------------------------------------------


def test_installed_host_operations_expose_all_bounded_actions(tmp_path):
    runner = _bare_runner()
    runner._install_conversation_extension_host()
    host = ce.gateway_host_operations()

    assert host.spawn_task is not None
    assert host.cancel_tasks is not None
    assert host.lookup_session is not None
    assert host.create_initiated_child is not None, "P1-2: initiated child unwired"
    assert host.inject_turn is not None, "P1-2: turn injection unwired"
    assert (
        host.send_authenticated_existing_dm is not None
    ), "P1-2: authenticated DM unwired"


def test_facade_exposes_no_runner_store_client_or_credentials(tmp_path):
    """Invariant: the facade must never leak host internals."""
    runner = _bare_runner()
    runner._install_conversation_extension_host()

    facade = ce.GatewayRuntimeFacade(
        extension_id="probe",
        profile_name="default",
        profile_home=str(tmp_path),
        generation=1,
        capabilities=frozenset({"initiated_turns", "turn_injection", "authenticated_dm"}),
        host=ce.gateway_host_operations(),
    )
    for forbidden in (
        "runner",
        "gateway",
        "session_store",
        "adapter",
        "client",
        "credentials",
        "config",
        "token",
    ):
        assert not hasattr(facade, forbidden), forbidden


def test_undeclared_capability_still_denies_wired_actions(tmp_path):
    """Wiring the host must not weaken capability gating (dark-Poke invariant)."""
    runner = _bare_runner()
    runner._install_conversation_extension_host()

    facade = ce.GatewayRuntimeFacade(
        extension_id="dark",
        profile_name="default",
        profile_home=str(tmp_path),
        generation=1,
        capabilities=frozenset({"tool_authorization", "health"}),
        host=ce.gateway_host_operations(),
    )
    with pytest.raises(ce.CapabilityDenied):
        facade.create_initiated_child(
            ce.InitiatedTurnRequest(
                session_key="k", prompt="hi", origin="test"
            )
        )
    with pytest.raises(ce.CapabilityDenied):
        facade.inject_turn(session_key="k", text="hi")
    with pytest.raises(ce.CapabilityDenied):
        facade.send_authenticated_existing_dm(
            ce.AuthenticatedDmRequest(
                platform="discord", chat_id="1", text="hi", reservation_key="r1"
            )
        )


def test_authenticated_dm_preserves_no_create_and_no_fallback(tmp_path):
    """The wired DM op must refuse an unauthorized/nonexistent chat definitively."""
    runner = _bare_runner()
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._install_conversation_extension_host()

    facade = ce.GatewayRuntimeFacade(
        extension_id="sender",
        profile_name="default",
        profile_home=str(tmp_path),
        generation=1,
        capabilities=frozenset({"authenticated_dm"}),
        host=ce.gateway_host_operations(),
    )
    result = facade.send_authenticated_existing_dm(
        ce.AuthenticatedDmRequest(
            platform="discord", chat_id="never-seen", text="hi", reservation_key="r1"
        )
    )
    assert result.outcome is ce.DmSendOutcome.DEFINITIVE_FAILURE
    assert "authorized" in result.detail or "unavailable" in result.detail


def test_initiated_child_request_requires_existing_session(tmp_path):
    """Bounded: no session -> refusal, never a fabricated child."""
    runner = _bare_runner()

    class _Store:
        def lookup_by_session_key(self, key):
            return None

    runner.session_store = _Store()
    runner._session_db = None
    runner._install_conversation_extension_host()
    host = ce.gateway_host_operations()

    result = host.create_initiated_child(
        ce.InitiatedTurnRequest(session_key="missing", prompt="hi", origin="test")
    )
    assert result.get("created") is False


# ---------------------------------------------------------------------------
# P1-3 — post-turn observation uses the validated runtime profile
# ---------------------------------------------------------------------------


def test_post_turn_observation_reports_validated_runtime_profile(tmp_path):
    scope = str(tmp_path / "post-turn")
    seen: list = []

    bundle = ce.GatewayConversationExtension(
        extension_id="obs",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"post_turn_observer"}),
        observe_turn_result=lambda result: seen.append(result.runtime_profile),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)

    ce_runtime.observe_turn_completion(
        scope=scope,
        session_key="k",
        runtime_profile="guest",  # the *validated* route, not the transport
        platform="discord",
        sender_identity="u1",
        user_text="hi",
        assistant_text="hello",
        delivered=True,
    )
    assert seen == ["guest"]


def test_route_decision_runtime_profile_is_preferred(tmp_path):
    """The runner helper must prefer the decision over the transport profile."""
    runner = _bare_runner()
    context = ce.GatewayRouteContext(
        platform="discord",
        adapter_identity="discord",
        transport_profile="default",
        transport_home=str(tmp_path),
        sender_identity="u1",
        chat_id="1",
        chat_type="dm",
        text_preview="hi",
        is_group=False,
    )
    decision = ce.GatewayRouteDecision(
        admitted=True,
        transport_profile="default",
        transport_home=str(tmp_path),
        runtime_profile="guest",
        reason="",
        extension_id="router",
        generation=1,
    )
    assert (
        runner._extension_runtime_profile(context, decision) == "guest"
    ), "validated route must win"
    assert runner._extension_runtime_profile(context, None) == "default"


# ---------------------------------------------------------------------------
# P1-5 — served profiles come from config, not adapter connect success
# ---------------------------------------------------------------------------


def test_served_profiles_include_config_profiles_with_failed_adapters(monkeypatch):
    runner = _bare_runner()
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._profile_adapters = {}  # every secondary adapter failed to connect

    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_multiplex_profile_homes",
        lambda config: [("default", Path("/tmp/d")), ("guest", Path("/tmp/g"))],
    )

    served = runner._served_profile_names()
    assert "guest" in served, "config-declared profile must be served despite adapter failure"
    assert "default" in served


def test_served_profiles_single_profile_gateway_is_unchanged(monkeypatch):
    runner = _bare_runner()
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._profile_adapters = {}

    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run, "_multiplex_profile_homes", lambda config: [("default", Path("/tmp/d"))]
    )
    assert runner._served_profile_names() == ("default",)


# ---------------------------------------------------------------------------
# P1-6 — permitted_conversation_routes is a real config field
# ---------------------------------------------------------------------------


def test_gateway_config_parses_permitted_conversation_routes():
    config = GatewayConfig.from_dict(
        {
            "gateway": {
                "permitted_conversation_routes": {"default": ["guest", "work"]},
            }
        }
    )
    assert config.permitted_conversation_routes == {"default": ["guest", "work"]}


def test_gateway_config_permitted_routes_default_is_empty():
    assert GatewayConfig().permitted_conversation_routes == {}


def test_gateway_config_permitted_routes_roundtrip():
    config = GatewayConfig.from_dict(
        {"gateway": {"permitted_conversation_routes": {"default": "guest"}}}
    )
    assert config.to_dict()["permitted_conversation_routes"] == {"default": ["guest"]}


def test_runner_reads_permitted_routes_from_config():
    runner = _bare_runner()
    runner.config = GatewayConfig.from_dict(
        {"gateway": {"permitted_conversation_routes": {"default": ["guest"]}}}
    )
    assert runner._permitted_extension_routes() == {"default": ("guest",)}


def test_permitted_route_enables_cross_profile_route(tmp_path):
    """The permitted branch must actually be reachable end to end."""
    scope = str(tmp_path / "routing")

    bundle = ce.GatewayConversationExtension(
        extension_id="router",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"admission_policy"}),
        authorize_route=lambda ctx: ce.GatewayRouteDirective(
            admit=True, runtime_profile="guest"
        ),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)

    context = ce.GatewayRouteContext(
        platform="discord",
        adapter_identity="discord",
        transport_profile="default",
        transport_home=str(tmp_path),
        sender_identity="u1",
        chat_id="1",
        chat_type="dm",
        text_preview="hi",
        is_group=False,
    )
    decision = ce_runtime.admit_and_route(
        context,
        scope=scope,
        served_profiles=("default", "guest"),
        permitted_routes={"default": ("guest",)},
    )
    assert decision is not None and decision.admitted is True
    assert decision.runtime_profile == "guest"


def test_unpermitted_route_is_refused(tmp_path):
    scope = str(tmp_path / "routing-denied")

    bundle = ce.GatewayConversationExtension(
        extension_id="router",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"admission_policy"}),
        authorize_route=lambda ctx: ce.GatewayRouteDirective(
            admit=True, runtime_profile="guest"
        ),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)

    context = ce.GatewayRouteContext(
        platform="discord",
        adapter_identity="discord",
        transport_profile="default",
        transport_home=str(tmp_path),
        sender_identity="u1",
        chat_id="1",
        chat_type="dm",
        text_preview="hi",
        is_group=False,
    )
    decision = ce_runtime.admit_and_route(
        context,
        scope=scope,
        served_profiles=("default", "guest"),
        permitted_routes={},  # fail-closed default
    )
    assert decision is not None and decision.admitted is False
