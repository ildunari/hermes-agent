"""Platform-owned status policy remains scoped and does not filter replies."""
from gateway.config import Platform
from gateway.platform_registry import PlatformEntry, PlatformRegistry
from gateway.run import _prepare_gateway_status_message, _sanitize_gateway_final_response


def test_registered_status_policy_is_scoped_and_preserves_other_delivery(monkeypatch):
    import gateway.platform_registry as registry_module
    registry = PlatformRegistry()
    monkeypatch.setattr(registry_module, "platform_registry", registry)
    current_scope = ["poke"]
    monkeypatch.setattr(registry, "current_scope_key", lambda: current_scope[0])
    for scope, suppressed in (("poke", {"*"}), ("guest", {"*"}), ("default", {"lifecycle"})):
        registry.register(PlatformEntry(name="bluebubbles", label="iMessage",
                                       adapter_factory=lambda cfg: None, check_fn=lambda: True,
                                       suppress_status_event_types=frozenset(suppressed)), scope=scope)
    message = "A provider warning that may also appear in an ordinary reply."
    for scope in ("poke", "guest"):
        current_scope[0] = scope
        for kind in ("lifecycle", "warn", "compacted", "notice", "future-status-kind"):
            assert _prepare_gateway_status_message(Platform.BLUEBUBBLES, kind, message) is None
        assert _sanitize_gateway_final_response(Platform.BLUEBUBBLES, message) == message
        assert _prepare_gateway_status_message(Platform.TELEGRAM, "warn", message) == message
    current_scope[0] = "default"
    assert _prepare_gateway_status_message(Platform.BLUEBUBBLES, "lifecycle", message) is None
    assert _prepare_gateway_status_message(Platform.BLUEBUBBLES, "warn", message) == message
