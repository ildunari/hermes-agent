"""KEEP glue: poke lifecycle host-order and default-transport BlueBubbles admission."""

from __future__ import annotations

from pathlib import Path
import pytest

from gateway import conversation_extension_host as ce_host
from gateway import conversation_extension_runtime as ce_runtime
from gateway import conversation_extensions as ce
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import (
    SessionSource,
    build_session_key,
    is_shared_multi_user_session,
)


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


def _bare_runner() -> GatewayRunner:
    return object.__new__(GatewayRunner)


class _PokeLike:
    """Mimic poke's on_start: fail-closed spawn, skip duplicate unless failed."""

    def __init__(self):
        self._started = False
        self._started_generation = None
        self._watcher_failed = False

    def on_start(self, facade) -> None:
        generation = getattr(facade, "generation", None)
        failed = getattr(self, "_watcher_failed", False)
        if (
            self._started
            and not failed
            and getattr(self, "_started_generation", object()) == generation
        ):
            return
        self._started = True
        self._started_generation = generation
        try:
            facade.spawn_lifecycle_task("proactive-watcher", lambda: None)
        except Exception:
            self._watcher_failed = True
            return
        self._watcher_failed = False

    def health(self):
        if getattr(self, "_watcher_failed", False):
            return ce.GatewayExtensionHealth(False, "proactive_watcher_unavailable")
        return ce.GatewayExtensionHealth(True, "authoritative")


def test_plugin_on_start_after_host_install_leaves_poke_healthy(tmp_path):
    """Failed first start (no host) must retry once spawn_task exists."""
    owner = _PokeLike()
    scope = str(tmp_path / "poke")
    bundle = ce.GatewayConversationExtension(
        extension_id="poke",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"lifecycle", "health", "admission_policy"}),
        on_start=owner.on_start,
        health=owner.health,
        authorize_route=lambda ctx: ce.GatewayRouteDirective(admit=True),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)

    facade = ce.GatewayRuntimeFacade(
        extension_id="poke",
        profile_name="poke",
        profile_home=scope,
        generation=1,
        capabilities=bundle.capabilities,
        host=ce.gateway_host_operations(),
    )
    owner.on_start(facade)
    assert owner.health().healthy is False

    spawned = []

    def _spawn(task):
        spawned.append(task.task_key)

    ce.install_gateway_host_operations(ce.GatewayHostOperations(spawn_task=_spawn))
    live = ce.GatewayRuntimeFacade(
        extension_id="poke",
        profile_name="poke",
        profile_home=scope,
        generation=1,
        capabilities=bundle.capabilities,
        host=ce.gateway_host_operations(),
    )
    owner.on_start(live)
    assert spawned == ["proactive-watcher"]
    assert owner.health().healthy is True


def test_early_host_install_sets_spawn_task_before_on_start():
    """Host-order path: start_gateway installs spawn_task before plugin load."""
    ce_host.install_early_lifecycle_scheduling()
    host = ce.gateway_host_operations()
    assert host.spawn_task is not None

    spawned = []
    ce.install_gateway_host_operations(
        ce.GatewayHostOperations(spawn_task=lambda task: spawned.append(task.task_key))
    )
    owner = _PokeLike()
    facade = ce.GatewayRuntimeFacade(
        extension_id="poke",
        profile_name="poke",
        profile_home="/tmp/poke",
        generation=1,
        capabilities=frozenset({"lifecycle", "health"}),
        host=ce.GatewayHostOperations(),  # stale snapshot; live host wins
    )
    owner.on_start(facade)
    assert spawned == ["proactive-watcher"]
    assert owner.health().healthy is True


def test_inbound_bluebubbles_owner_dm_on_default_admits_to_poke(tmp_path, monkeypatch):
    """Default-transport owner iMessage is admitted to poke, with owner prefix."""
    poke_home = tmp_path / "profiles" / "poke"
    poke_home.mkdir(parents=True)
    default_home = tmp_path / "default"
    default_home.mkdir()

    runner = _bare_runner()
    runner.config = GatewayConfig.from_dict(
        {
            "gateway": {
                "multiplex_profiles": True,
                "permitted_conversation_routes": {"default": ["poke", "guest"]},
            }
        }
    )
    runner.config.multiplex_profiles = True

    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_multiplex_profile_homes",
        lambda config: [
            ("default", default_home),
            ("poke", poke_home),
            ("guest", tmp_path / "guest"),
        ],
    )

    from hermes_constants import hermes_home_key

    poke_scope = hermes_home_key(str(poke_home))

    def _authorize(ctx):
        return ce.GatewayRouteDirective(
            admit=True,
            runtime_profile="poke",
            principal="owner",
            subject_id="kosta-owner",
            context_prefix="[Owner contact context: contact_id=kosta-owner; principal=owner; platform=bluebubbles] ",
            reason="owner sender",
        )

    bundle = ce.GatewayConversationExtension(
        extension_id="poke",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"admission_policy"}),
        authorize_route=_authorize,
    )
    ce.conversation_extension_registry.register(bundle, scope=poke_scope)

    source = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id="+18155554543",
        chat_type="dm",
        user_id="+18155554543",
    )
    event = MessageEvent(text="Yo", source=source)

    transport_home = str(default_home)
    scope, requirements_profile = runner._admission_scope_for_source(
        source, transport_home
    )
    assert requirements_profile == "poke"
    assert scope == poke_scope

    context = ce_runtime.build_route_context(
        event,
        transport_profile="default",
        transport_home=transport_home,
    )
    assert context is not None
    decision = ce_runtime.admit_and_route(
        context,
        scope=scope,
        served_profiles=("default", "poke", "guest"),
        permitted_routes=runner._permitted_extension_routes(),
    )
    assert decision is not None
    assert decision.admitted is True
    assert decision.runtime_profile == "poke"
    assert decision.principal == "owner"

    routed_source, routed_event = runner._apply_extension_route_decision(
        source, event, decision
    )
    assert routed_source.profile == "poke"
    assert routed_event.text.startswith("[Owner contact context:")
    assert build_session_key(
        routed_source, profile=routed_source.profile
    ).startswith("agent:poke:bluebubbles:dm:")


def test_inbound_bluebubbles_guest_dm_on_default_admits_to_guest(
    tmp_path, monkeypatch
):
    """Default-transport approved guest iMessage enters the guest namespace."""
    poke_home = tmp_path / "profiles" / "poke"
    poke_home.mkdir(parents=True)
    default_home = tmp_path / "default"
    default_home.mkdir()

    runner = _bare_runner()
    runner.config = GatewayConfig.from_dict(
        {
            "gateway": {
                "multiplex_profiles": True,
                "permitted_conversation_routes": {"default": ["poke", "guest"]},
            }
        }
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_multiplex_profile_homes",
        lambda config: [
            ("default", default_home),
            ("poke", poke_home),
            ("guest", tmp_path / "profiles" / "guest"),
        ],
    )

    from hermes_constants import hermes_home_key

    poke_scope = hermes_home_key(str(poke_home))
    bundle = ce.GatewayConversationExtension(
        extension_id="poke",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"admission_policy"}),
        authorize_route=lambda ctx: ce.GatewayRouteDirective(
            admit=True,
            runtime_profile="guest",
            principal="guest",
            subject_id="steve",
            context_prefix="[Guest contact context: approved_contact_id=steve] ",
            reason="approved guest sender",
        ),
    )
    ce.conversation_extension_registry.register(bundle, scope=poke_scope)

    source = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id="steve@example.test",
        chat_type="dm",
        user_id="steve@example.test",
    )
    event = MessageEvent(text="Hey", source=source)
    transport_home = str(default_home)
    scope, requirements_profile = runner._admission_scope_for_source(
        source, transport_home
    )

    assert requirements_profile == "poke"
    assert scope == poke_scope
    context = ce_runtime.build_route_context(
        event,
        transport_profile="default",
        transport_home=transport_home,
    )
    decision = ce_runtime.admit_and_route(
        context,
        scope=scope,
        served_profiles=("default", "poke", "guest"),
        permitted_routes=runner._permitted_extension_routes(),
    )

    assert decision is not None and decision.admitted is True
    assert decision.runtime_profile == "guest"
    routed_source, routed_event = runner._apply_extension_route_decision(
        source, event, decision
    )
    assert routed_source.profile == "guest"
    assert routed_event.text.startswith("[Guest contact context:")
    assert build_session_key(
        routed_source, profile=routed_source.profile
    ).startswith("agent:guest:bluebubbles:dm:")


def test_gateway_config_parses_permitted_conversation_routes():
    config = GatewayConfig.from_dict(
        {"gateway": {"permitted_conversation_routes": {"default": ["poke", "guest"]}}}
    )
    assert config.permitted_conversation_routes == {"default": ["poke", "guest"]}


def test_gateway_config_permitted_routes_default_is_empty():
    assert GatewayConfig().permitted_conversation_routes == {}


def test_bluebubbles_group_session_remains_shared_after_main_land():
    first = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id="iMessage;+;group-guid",
        chat_type="group",
        user_id="member-one",
        chat_id_alt="hermes-profile:guest",
    )
    second = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id="iMessage;+;group-guid",
        chat_type="group",
        user_id="member-two",
        chat_id_alt="hermes-profile:guest",
    )

    assert build_session_key(first, profile="guest") == build_session_key(
        second, profile="guest"
    )
    assert build_session_key(first, profile="guest").startswith(
        "agent:guest:bluebubbles:group:"
    )
    assert is_shared_multi_user_session(first) is True
