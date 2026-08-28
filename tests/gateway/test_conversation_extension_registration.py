"""Plugin-facing registration lifecycle for gateway conversation extensions.

Covers the seam between ``PluginContext.register_gateway_conversation_extension``
and the core registry: tracked unload, generation safety, profile isolation,
and no-plugin behavior.
"""

from __future__ import annotations

import threading

import pytest
import yaml

from gateway import conversation_extensions as ce
from gateway.readiness import collect_runtime_readiness


def _bundle(extension_id="probe", **kwargs) -> ce.GatewayConversationExtension:
    kwargs.setdefault(
        "authorize_tool", lambda request: ce.GatewayToolAuthorizationDecision(True)
    )
    capabilities = kwargs.pop("capabilities", ("tool_authorization",))
    return ce.GatewayConversationExtension(
        extension_id=extension_id,
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset(capabilities),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _clean():
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    yield
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()


class _FakeManager:
    def __init__(self, scope_key: str):
        self.scope_key = scope_key
        self.tracked: list = []

    def _track_registration(self, manifest, kind, key, release, persistent=False):
        from hermes_cli.plugins import PluginRegistration

        registration = PluginRegistration(
            kind=kind, key=key, release=release, plugin_key=getattr(manifest, "name", "")
        )
        self.tracked.append(registration)
        return registration


def _context(scope: str):
    """Build a PluginContext bound to a fake manager for *scope*."""
    from hermes_cli.plugins import PluginContext

    context = PluginContext.__new__(PluginContext)
    context._manager = _FakeManager(scope)
    context.manifest = type("_Manifest", (), {"name": "probe-plugin", "key": "probe"})()
    return context


# ---------------------------------------------------------------------------
# registration through the public plugin API
# ---------------------------------------------------------------------------


def test_register_through_plugin_context_makes_extension_active():
    context = _context("/home/a")
    handle = context.register_gateway_conversation_extension(_bundle())

    assert handle is not None
    assert handle.active is True
    assert ce.conversation_extension_registry.get("probe", scope="/home/a") is not None


def test_dispose_handle_unregisters_extension():
    context = _context("/home/a")
    handle = context.register_gateway_conversation_extension(_bundle())

    handle.dispose()
    assert ce.conversation_extension_registry.get("probe", scope="/home/a") is None
    assert handle.active is False


def test_repeated_dispose_is_harmless():
    context = _context("/home/a")
    handle = context.register_gateway_conversation_extension(_bundle())
    handle.dispose()
    handle.dispose()
    assert ce.conversation_extension_registry.get("probe", scope="/home/a") is None


def test_invalid_bundle_is_rejected_without_registering():
    context = _context("/home/a")
    assert context.register_gateway_conversation_extension(object()) is None
    assert ce.conversation_extension_registry.active_ids(scope="/home/a") == ()


def test_registration_is_profile_scoped_across_managers():
    context_a = _context("/home/a")
    context_b = _context("/home/b")
    context_a.register_gateway_conversation_extension(_bundle())

    assert ce.conversation_extension_registry.get("probe", scope="/home/a") is not None
    assert ce.conversation_extension_registry.get("probe", scope="/home/b") is None

    context_b.register_gateway_conversation_extension(_bundle())
    assert ce.conversation_extension_registry.get("probe", scope="/home/b") is not None


def test_stale_generation_unload_cannot_clear_a_reload():
    """Plugin reload while tasks are active: old generation must not win."""
    context = _context("/home/a")
    first = context.register_gateway_conversation_extension(_bundle())
    second_bundle = _bundle()
    second = context.register_gateway_conversation_extension(second_bundle)

    # Old generation tears down late.
    first.dispose()

    assert ce.conversation_extension_registry.get("probe", scope="/home/a") is second_bundle
    assert second.active is True

    second.dispose()
    assert ce.conversation_extension_registry.get("probe", scope="/home/a") is None


def test_concurrent_registration_and_unload_is_consistent():
    context = _context("/home/a")
    handles = []
    barrier = threading.Barrier(6)

    def _worker():
        barrier.wait()
        handles.append(context.register_gateway_conversation_extension(_bundle()))

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(handles) == 6
    active_generation = ce.conversation_extension_registry.active_generation(
        "probe", scope="/home/a"
    )
    assert active_generation is not None

    for handle in handles:
        handle.dispose()
    assert ce.conversation_extension_registry.get("probe", scope="/home/a") is None


# ---------------------------------------------------------------------------
# readiness integration (fail closed per profile)
# ---------------------------------------------------------------------------


def _write_config(home, required):
    home.mkdir(parents=True, exist_ok=True)
    payload = {"gateway": {"required_conversation_extensions": required}}
    (home / "config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def _readiness(monkeypatch, home):
    monkeypatch.setattr("gateway.readiness.get_hermes_home", lambda: home)
    return collect_runtime_readiness(configured_model="test-model", runtime_status={})


def test_profile_without_requirements_is_ready(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    report = _readiness(monkeypatch, home)
    assert report["checks"]["conversation_extensions"]["status"] == "ok"


def test_profile_requiring_missing_extension_is_unready(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _write_config(home, [{"id": "probe", "api_version": 1}])
    report = _readiness(monkeypatch, home)
    check = report["checks"]["conversation_extensions"]
    assert check["status"] == "degraded"
    assert report["status"] == "degraded"


def test_profile_requiring_present_extension_is_ready(monkeypatch, tmp_path):
    from hermes_constants import hermes_home_key

    home = tmp_path / "home"
    _write_config(home, [{"id": "probe", "api_version": 1}])
    ce.conversation_extension_registry.register(
        _bundle(), scope=hermes_home_key(home)
    )
    report = _readiness(monkeypatch, home)
    assert report["checks"]["conversation_extensions"]["status"] == "ok"


def test_malformed_requirement_declaration_is_unready(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _write_config(home, [{"api_version": 1}])  # missing id
    report = _readiness(monkeypatch, home)
    assert report["checks"]["conversation_extensions"]["status"] == "degraded"


def test_requirement_satisfied_only_in_another_profile_is_unready(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _write_config(home, [{"id": "probe", "api_version": 1}])
    ce.conversation_extension_registry.register(_bundle(), scope="/home/somewhere-else")
    report = _readiness(monkeypatch, home)
    assert report["checks"]["conversation_extensions"]["status"] == "degraded"


def test_readiness_payload_does_not_leak_config_or_messages(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _write_config(home, [{"id": "secret-extension-name", "api_version": 1}])
    report = _readiness(monkeypatch, home)
    payload = report["checks"]["conversation_extensions"]
    # Extension ids are operator-facing identifiers, but no paths/exception
    # text may appear.
    serialized = str(payload)
    assert str(tmp_path) not in serialized
    assert "Traceback" not in serialized


# ---------------------------------------------------------------------------
# no-plugin compatibility
# ---------------------------------------------------------------------------


def test_core_behaves_normally_with_no_extension_registered():
    assert ce.conversation_extension_registry.active_ids(scope="/home/a") == ()
    assert ce.authorize_tool_dispatch("terminal", {"command": "ls"}) is None

    decision = ce.resolve_route(
        ce.GatewayRouteContext(
            platform="telegram",
            adapter_identity="tg:bot",
            transport_profile="root",
            transport_home="/home/a",
            sender_identity="+1555",
            chat_id="c1",
            chat_type="dm",
        ),
        scope="/home/a",
        served_profiles=("root",),
    )
    assert decision.admitted is True
    assert decision.runtime_profile == "root"

    augmentation = ce.collect_turn_augmentation(
        ce.GatewayTurnContext(
            session_key="s1",
            runtime_profile="root",
            platform="telegram",
            sender_identity="+1555",
            chat_type="dm",
            user_text="hi",
        ),
        scope="/home/a",
    )
    assert augmentation.user_context == ()
    assert augmentation.degraded is False

    report = ce.evaluate_extension_readiness(required=(), scope="/home/a")
    assert report.ready is True
