from types import SimpleNamespace

import pytest

from cron import scheduler
from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry


def _entry(validator=None):
    return SimpleNamespace(cron_delivery_validator_fn=validator)


def _disable_discovery(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)


def test_platform_entry_exposes_optional_cron_delivery_validator():
    validator = lambda job, target: True
    entry = PlatformEntry(
        name="example",
        label="Example",
        adapter_factory=lambda config: object(),
        check_fn=lambda: True,
        cron_delivery_validator_fn=validator,
    )

    assert entry.cron_delivery_validator_fn is validator


def test_absent_validator_preserves_ordinary_delivery(monkeypatch):
    _disable_discovery(monkeypatch)
    monkeypatch.setattr(
        "gateway.platform_registry.platform_registry.get",
        lambda platform: _entry(None),
    )

    errors = scheduler._cron_delivery_validation_errors(
        {"id": "ordinary"},
        [{"platform": "example", "chat_id": "target"}],
    )

    assert errors == [None]


def test_validator_receives_copies_and_true_allows(monkeypatch):
    _disable_discovery(monkeypatch)
    job = {"id": "job", "nested": {"preserved": True}}
    target = {"platform": "example", "chat_id": "target"}

    def validator(job_copy, target_copy):
        job_copy["id"] = "mutated"
        target_copy["chat_id"] = "mutated"
        return True

    monkeypatch.setattr(
        "gateway.platform_registry.platform_registry.get",
        lambda platform: _entry(validator),
    )

    assert scheduler._cron_delivery_validation_errors(job, [target]) == [None]
    assert job["id"] == "job"
    assert target["chat_id"] == "target"


@pytest.mark.parametrize(
    ("result", "diagnostic"),
    [
        (False, "validator rejected target"),
        ("policy says no", "policy says no"),
        (None, "malformed NoneType result"),
        ("", "malformed str result"),
        (1, "malformed int result"),
    ],
)
def test_validator_rejections_and_malformed_results_fail_closed(
    monkeypatch, result, diagnostic,
):
    _disable_discovery(monkeypatch)
    monkeypatch.setattr(
        "gateway.platform_registry.platform_registry.get",
        lambda platform: _entry(lambda job, target: result),
    )

    errors = scheduler._cron_delivery_validation_errors(
        {"id": "job"},
        [{"platform": "example", "chat_id": "target"}],
    )

    assert errors[0] is not None
    assert diagnostic in errors[0]


def test_validator_exception_fails_closed_without_leaking_exception_text(monkeypatch):
    _disable_discovery(monkeypatch)

    def validator(job, target):
        raise RuntimeError("secret-bearing detail")

    monkeypatch.setattr(
        "gateway.platform_registry.platform_registry.get",
        lambda platform: _entry(validator),
    )

    errors = scheduler._cron_delivery_validation_errors(
        {"id": "job"},
        [{"platform": "example", "chat_id": "target"}],
    )

    assert errors == [
        "cron delivery validator for platform 'example' failed closed "
        "after raising RuntimeError"
    ]


@pytest.mark.parametrize(
    ("adapters", "loop"),
    [
        (None, None),
        ({"example": object()}, SimpleNamespace(is_running=lambda: True)),
    ],
    ids=["standalone-route", "live-adapter-route"],
)
def test_rejected_target_stops_before_any_route_or_config_load(
    monkeypatch, adapters, loop,
):
    _disable_discovery(monkeypatch)
    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda job: [{"platform": "example", "chat_id": "blocked"}],
    )
    monkeypatch.setattr(
        "gateway.platform_registry.platform_registry.get",
        lambda platform: _entry(lambda job, target: "example policy rejected target"),
    )
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: pytest.fail("config loaded after every target was rejected"),
    )

    error = scheduler._deliver_result(
        {"id": "job", "name": "Internal job", "deliver": "example:blocked"},
        "output",
        adapters=adapters,
        loop=loop,
    )

    assert error == "example policy rejected target"


def test_broadcast_validates_all_targets_and_never_sends_rejected_target(monkeypatch):
    _disable_discovery(monkeypatch)
    events = []
    targets = [
        {"platform": "discord", "chat_id": "allowed"},
        {"platform": "slack", "chat_id": "blocked"},
    ]

    def entry_for(platform):
        if platform == "discord":
            return _entry(lambda job, target: events.append("validate-discord") or True)
        if platform == "slack":
            return _entry(
                lambda job, target: events.append("validate-slack")
                or "slack policy rejected target"
            )
        return None

    config = SimpleNamespace(
        platforms={
            Platform.DISCORD: PlatformConfig(enabled=True, token="discord-token"),
            Platform.SLACK: PlatformConfig(enabled=True, token="slack-token"),
        }
    )

    async def fake_send(platform, pconfig, chat_id, message, **kwargs):
        events.append(f"send-{platform.value}-{chat_id}")
        return {"success": True, "message_id": "sent"}

    monkeypatch.setattr(scheduler, "_resolve_delivery_targets", lambda job: targets)
    monkeypatch.setattr(
        "gateway.platform_registry.platform_registry.get", entry_for
    )
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr(
        "gateway.delivery.resolve_delivery_transport", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("tools.send_message_tool._send_to_platform", fake_send)
    monkeypatch.setattr(
        scheduler, "load_config", lambda: {"cron": {"wrap_response": False}}
    )

    error = scheduler._deliver_result(
        {"id": "broadcast", "name": "Broadcast", "deliver": "all"},
        "output",
    )

    assert error == "slack policy rejected target"
    assert events == [
        "validate-discord",
        "validate-slack",
        "send-discord-allowed",
    ]
