"""Bounded relay queue and exactly-once browser operation lifecycle tests."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from typing import Any

import pytest

from hermes_cli.browser_transport import (
    APPLICATION_MAX_MESSAGE_BYTES,
    CONNECTION_MAX_OUTSTANDING,
    DEFAULT_TRANSPORT_LIMITS,
    RELAY_MAX_OUTSTANDING,
    RELAY_QUEUE_MAX_BYTES,
    RELAY_QUEUE_MAX_MESSAGES,
    TEARDOWN_RESERVE_MESSAGES,
    WEBSOCKET_MAX_MESSAGE_BYTES,
    BrowserProtocolError,
    BrowserTransportLimits,
    BrowserTransportManager,
    WIRE_CONTRACT,
    method_set_hash,
    validate_application_message_size,
)


def _connection_id() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


class FakeSocket:
    async def send_json(self, _payload: dict) -> None:
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


class RecordingWriter:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.frames: list[Any] = []

    async def __call__(self, serialized: bytes) -> bool:
        self.frames.append(json.loads(serialized))
        return self.accepted


class GateWriter(RecordingWriter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, serialized: bytes) -> bool:
        self.frames.append(json.loads(serialized))
        self.entered.set()
        await self.release.wait()
        return True


class ExplodingPayload:
    def __iter__(self):
        raise AssertionError("payload was accessed before the owner/id fence")


async def _uncertain_writer(_serialized: bytes) -> bool:
    raise OSError("private transport detail")


def _hello(connection_id: str, profile: str = "gpt") -> dict:
    return {
        "type": "client.hello",
        "profile": profile,
        "connection_id": connection_id,
        "browser": {
            "present": True,
            "local_enabled": True,
            "protocol": dict(WIRE_CONTRACT["protocol"]),
            "method_set_hash": method_set_hash(),
            "methods": [row["name"] for row in WIRE_CONTRACT["required_methods"]],
        },
    }


def _ready(*, limits: BrowserTransportLimits = DEFAULT_TRANSPORT_LIMITS, monotonic=None):
    manager = BrowserTransportManager(
        limits=limits,
        **({"monotonic": monotonic} if monotonic is not None else {}),
    )
    connection_id = _connection_id()
    manager.register_chat(
        transport=object(),
        principal="nous:queue-owner",
        profile="gpt",
        connection_id=connection_id,
    )
    context = manager.new_browser_context(
        transport=FakeSocket(),
        ticket={
            "principal": "nous:queue-owner",
            "profile": "gpt",
            "connection_id": connection_id,
        },
    )
    hello = manager.negotiate(context, _hello(connection_id), server_enabled=True)
    assert hello["status"] == "ready"
    return manager, context


def _fence(relay) -> dict:
    return {
        "transport": relay.context.transport,
        "sid": relay.sid,
        "profile": relay.profile,
        "capability_generation": relay.capability_generation,
        "relay_token": relay.relay_token,
        "binding_generation": relay.binding_generation,
    }


def _small_limits(**overrides) -> BrowserTransportLimits:
    values = {
        "application_message_bytes": 256,
        "queue_messages": 4,
        "queue_bytes": 512,
        "relay_outstanding": 4,
        "connection_outstanding": 8,
        "teardown_messages": 1,
        "teardown_bytes": 64,
    }
    values.update(overrides)
    return BrowserTransportLimits(**values)


def _write(manager, relay, writer=None):
    selected = writer or RecordingWriter()
    frame = asyncio.run(manager.write_next(relay, selected))
    return selected, frame


def test_release_limits_pin_64_mib_ws_50_mib_app_and_bounded_defaults():
    assert WEBSOCKET_MAX_MESSAGE_BYTES == 64 * 1024 * 1024
    assert APPLICATION_MAX_MESSAGE_BYTES == 50 * 1024 * 1024
    assert RELAY_QUEUE_MAX_MESSAGES == 32
    assert RELAY_QUEUE_MAX_BYTES == 64 * 1024 * 1024
    assert RELAY_MAX_OUTSTANDING == 32
    assert CONNECTION_MAX_OUTSTANDING == 64
    assert TEARDOWN_RESERVE_MESSAGES == 1
    assert validate_application_message_size(b"12345678", limit=8) == 8
    with pytest.raises(BrowserProtocolError) as exc:
        validate_application_message_size(b"123456789", limit=8)
    assert exc.value.code == "browser_frame_too_large"
    assert exc.value.outcome()["delivery"] == "not_started"


def test_each_direction_is_fifo_and_independently_count_and_byte_bounded():
    limits = _small_limits(
        application_message_bytes=60,
        queue_messages=3,
        queue_bytes=70,
        teardown_bytes=10,
    )
    manager, context = _ready(limits=limits)
    relay = manager.bind_relay(context, target_id="one-target")

    manager.enqueue_frame(relay, {"n": 1, "data": "a" * 10}, direction="inbound")
    manager.enqueue_frame(relay, {"n": 2, "data": "b" * 10}, direction="inbound")
    with pytest.raises(BrowserProtocolError) as count_exc:
        manager.enqueue_frame(relay, {"n": 3}, direction="inbound")
    assert count_exc.value.code == "browser_overloaded"
    assert count_exc.value.retryable is True

    assert relay.inbound.get().decode()["n"] == 1
    assert relay.inbound.get().decode()["n"] == 2
    assert relay.inbound.get() is None

    manager.enqueue_frame(relay, {"data": "a" * 30}, direction="outbound")
    with pytest.raises(BrowserProtocolError) as byte_exc:
        manager.enqueue_frame(relay, {"data": "b" * 30}, direction="outbound")
    assert byte_exc.value.code == "browser_overloaded"
    assert relay.inbound.message_count == 0
    assert relay.outbound.message_count == 1


def test_write_couples_dequeue_acceptance_and_dispatch_state():
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context)
    operation = manager.admit_operation(relay, {"method": "Page.navigate"})
    writer = RecordingWriter()

    assert operation.state == "admitted"
    frame = asyncio.run(manager.write_next(relay, writer))
    assert frame is not None and frame.operation_id == operation.operation_id
    assert writer.frames == [{"method": "Page.navigate"}]
    assert operation.state == "dispatched"
    assert relay.outbound.message_count == 0
    assert not hasattr(manager, "mark_dispatched")


def test_preaccept_rejection_is_not_started_and_closes_relay():
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context)
    operation = manager.admit_operation(relay, {"method": "Page.navigate"})

    with pytest.raises(BrowserProtocolError) as exc:
        asyncio.run(manager.write_next(relay, RecordingWriter(accepted=False)))
    assert exc.value.code == "browser_write_rejected"
    assert operation.outcome["status"] == "browser_write_rejected"
    assert operation.outcome["delivery"] == "not_started"
    assert relay.state == "closed"
    with pytest.raises(BrowserProtocolError, match="not active"):
        manager.admit_operation(relay, {"method": "Runtime.evaluate"})


def test_writer_exception_is_uncertain_and_never_leaks_transport_detail():
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context)
    operation = manager.admit_operation(relay, {"method": "Page.navigate"})

    with pytest.raises(BrowserProtocolError) as exc:
        asyncio.run(manager.write_next(relay, _uncertain_writer))
    assert exc.value.code == "browser_write_uncertain"
    assert exc.value.outcome()["delivery"] == "outcome_unknown"
    assert "private transport detail" not in str(exc.value)
    assert operation.outcome["delivery"] == "outcome_unknown"
    assert operation.outcome["retryable"] is False
    assert relay.state == "closed"


def test_transport_loss_certainty_matrix_covers_queued_writing_and_dispatched():
    admitted_manager, admitted_context = _ready(limits=_small_limits())
    admitted_relay = admitted_manager.bind_relay(admitted_context)
    admitted = admitted_manager.admit_operation(
        admitted_relay, {"id": 1, "method": "Page.navigate"}
    )
    assert admitted_manager.close_relay(
        admitted_relay, status="browser_disconnected"
    ) is True
    admitted_outcome = admitted.outcome
    assert admitted_outcome is not None
    assert admitted_outcome["delivery"] == "not_started"
    assert admitted_outcome["retryable"] is True

    dispatched_manager, dispatched_context = _ready(limits=_small_limits())
    dispatched_relay = dispatched_manager.bind_relay(dispatched_context)
    dispatched = dispatched_manager.admit_operation(
        dispatched_relay, {"id": 2, "method": "Page.navigate"}
    )
    _write(dispatched_manager, dispatched_relay)
    assert dispatched_manager.close_relay(
        dispatched_relay, status="browser_disconnected"
    ) is True
    dispatched_outcome = dispatched.outcome
    assert dispatched_outcome is not None
    assert dispatched_outcome["delivery"] == "outcome_unknown"
    assert dispatched_outcome["retryable"] is False

    async def writing_loss() -> None:
        manager, context = _ready(limits=_small_limits())
        relay = manager.bind_relay(context)
        operation = manager.admit_operation(relay, {"id": 3, "method": "Page.navigate"})
        writer = GateWriter()
        dispatch = asyncio.create_task(manager.write_next(relay, writer))
        await writer.entered.wait()
        assert operation.state == "writing"
        assert manager.close_relay(relay, status="browser_disconnected") is True
        outcome = operation.outcome
        assert outcome is not None
        assert outcome["delivery"] == "outcome_unknown"
        assert outcome["retryable"] is False
        writer.release.set()
        await dispatch

    asyncio.run(writing_loss())


def test_cancel_uses_reserved_teardown_when_normal_queue_is_saturated():
    limits = _small_limits(queue_messages=3, relay_outstanding=4)
    manager, context = _ready(limits=limits)
    relay = manager.bind_relay(context, target_id="target-A")
    first = manager.admit_operation(relay, {"seq": 1})
    second = manager.admit_operation(relay, {"seq": 2})
    with pytest.raises(BrowserProtocolError) as exc:
        manager.admit_operation(relay, {"seq": 3})
    assert exc.value.code == "browser_overloaded"

    writer = RecordingWriter()
    assert asyncio.run(manager.cancel_operation(first, writer=writer)) is True
    assert writer.frames == [{"type": "browser.relay.close"}]
    assert first.outcome["delivery"] == "not_started"
    assert second.outcome["delivery"] == "not_started"
    assert relay.outbound.diagnostics() == {
        "messages": 0,
        "bytes": 0,
        "control_messages": 0,
        "control_bytes": 0,
    }
    assert relay.state == "closed"
    assert asyncio.run(manager.cancel_operation(first, writer=writer)) is False
    assert writer.frames == [{"type": "browser.relay.close"}]


def test_cancel_during_accepted_write_serializes_teardown_and_is_outcome_unknown():
    async def exercise():
        manager, context = _ready(limits=_small_limits())
        relay = manager.bind_relay(context)
        operation = manager.admit_operation(relay, {"method": "Page.navigate"})
        operation_writer = GateWriter()
        teardown_writer = RecordingWriter()

        dispatch = asyncio.create_task(manager.write_next(relay, operation_writer))
        await operation_writer.entered.wait()
        cancel = asyncio.create_task(manager.cancel_operation(operation, writer=teardown_writer))
        await asyncio.sleep(0)
        assert relay.state == "closing"
        with pytest.raises(BrowserProtocolError):
            manager.admit_operation(relay, {"method": "Runtime.evaluate"})
        operation_writer.release.set()
        await dispatch
        assert await cancel is True
        assert operation.outcome["status"] == "browser_cancelled"
        assert operation.outcome["delivery"] == "outcome_unknown"
        assert teardown_writer.frames == [{"type": "browser.relay.close"}]
        assert relay.state == "closed"

    asyncio.run(exercise())


@pytest.mark.parametrize("writer_result", [True, False])
def test_matching_response_during_writer_await_proves_dispatch_and_settles(writer_result):
    async def exercise():
        manager, context = _ready(limits=_small_limits())
        relay = manager.bind_relay(context)
        operation = manager.admit_operation(relay, {"method": "Runtime.evaluate"})

        async def responding_writer(_serialized: bytes) -> bool:
            assert operation.state == "writing"
            assert manager.accept_operation_response(
                relay,
                operation_id=operation.operation_id,
                payload={"result": {"value": 1}},
                **_fence(relay),
            )
            accepted_outcome = operation.outcome
            assert accepted_outcome is not None
            assert accepted_outcome["delivery"] == "confirmed"
            await asyncio.sleep(0)
            return writer_result

        frame = await manager.write_next(relay, responding_writer)
        assert frame is not None and frame.operation_id == operation.operation_id
        outcome = operation.outcome
        assert outcome is not None
        assert outcome["status"] == "completed"
        assert outcome["delivery"] == "confirmed"
        assert relay.outstanding_count == 0
        assert relay.state == "active"

    asyncio.run(exercise())


def test_cancelled_close_coordinator_always_finalizes_while_writer_holds_lock():
    async def exercise():
        manager, context = _ready(limits=_small_limits())
        relay = manager.bind_relay(context)
        operation = manager.admit_operation(relay, {"method": "Page.navigate"})
        operation_writer = GateWriter()

        dispatch = asyncio.create_task(manager.write_next(relay, operation_writer))
        await operation_writer.entered.wait()
        close_task = asyncio.create_task(
            manager.cancel_operation(operation, writer=RecordingWriter())
        )
        while relay.state != "closing":
            await asyncio.sleep(0)
        assert relay.outbound.control_count == 1

        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task

        assert relay.state == "closed"
        assert relay.outstanding_count == 0
        assert relay.outbound.diagnostics() == {
            "messages": 0,
            "bytes": 0,
            "control_messages": 0,
            "control_bytes": 0,
        }
        assert manager.live_relay_token_count == 0
        assert relay not in context.relays
        outcome = operation.outcome
        assert outcome is not None
        assert outcome["delivery"] == "outcome_unknown"

        operation_writer.release.set()
        await dispatch
        assert relay.state == "closed"
        final_outcome = operation.outcome
        assert final_outcome is not None
        assert final_outcome["delivery"] == "outcome_unknown"

    asyncio.run(exercise())


def test_response_cancel_and_response_timeout_races_are_exactly_once():
    async def response_first(close_kind: str):
        manager, context = _ready(limits=_small_limits())
        relay = manager.bind_relay(context)
        operation = manager.admit_operation(relay, {"method": "Runtime.evaluate"})
        await manager.write_next(relay, RecordingWriter())
        assert manager.accept_operation_response(
            relay,
            operation_id=operation.operation_id,
            payload={"result": {"value": 1}},
            **_fence(relay),
        )
        frozen = operation.outcome
        closer = manager.cancel_operation if close_kind == "cancel" else manager.timeout_operation
        assert await closer(operation, writer=RecordingWriter()) is True
        assert operation.outcome == frozen
        assert relay.state == "closed"

    async def close_first(close_kind: str):
        manager, context = _ready(limits=_small_limits())
        relay = manager.bind_relay(context)
        operation = manager.admit_operation(relay, {"method": "Runtime.evaluate"})
        await manager.write_next(relay, RecordingWriter())
        closer = manager.cancel_operation if close_kind == "cancel" else manager.timeout_operation
        assert await closer(operation, writer=RecordingWriter()) is True
        frozen = operation.outcome
        assert frozen["delivery"] == "outcome_unknown"
        with pytest.raises(BrowserProtocolError) as late:
            manager.accept_operation_response(
                relay,
                operation_id=operation.operation_id,
                payload={"result": {"value": 2}},
                **_fence(relay),
            )
        assert late.value.code == "browser_stale_binding_generation"
        assert operation.outcome == frozen

    for close_kind in ("cancel", "timeout"):
        asyncio.run(response_first(close_kind))
        asyncio.run(close_first(close_kind))


def test_interrupt_and_duplicate_close_are_idempotent_and_send_one_teardown():
    async def exercise():
        manager, context = _ready(limits=_small_limits())
        relay = manager.bind_relay(context)
        operation = manager.admit_operation(relay, {"method": "Page.navigate"})
        writer = RecordingWriter()
        results = await asyncio.gather(
            manager.interrupt_operation(operation, writer=writer),
            manager.cancel_operation(operation, writer=writer),
        )
        assert sorted(results) == [False, True]
        assert writer.frames == [{"type": "browser.relay.close"}]
        assert operation.outcome["delivery"] == "not_started"
        assert relay.state == "closed"

    asyncio.run(exercise())


def test_per_relay_and_connection_outstanding_limits_reject_before_dispatch():
    limits = _small_limits(queue_messages=4, relay_outstanding=2, connection_outstanding=3)
    manager, context = _ready(limits=limits)
    first_relay = manager.bind_relay(context, target_id="target-A", task_id="task-A", tab_id="tab-A")
    second_relay = manager.bind_relay(context, target_id="target-B", task_id="task-B", tab_id="tab-B")
    manager.admit_operation(first_relay, {"n": 1})
    manager.admit_operation(first_relay, {"n": 2})
    with pytest.raises(BrowserProtocolError) as relay_exc:
        manager.admit_operation(first_relay, {"n": 3})
    assert relay_exc.value.code == "browser_overloaded"
    assert relay_exc.value.outcome()["delivery"] == "not_started"

    manager.admit_operation(second_relay, {"n": 1})
    with pytest.raises(BrowserProtocolError) as connection_exc:
        manager.admit_operation(second_relay, {"n": 2})
    assert connection_exc.value.code == "browser_overloaded"
    assert first_relay.outstanding_count == 2
    assert second_relay.outstanding_count == 1


def test_manager_owned_operation_ids_do_not_reuse_after_257_operations():
    limits = _small_limits(relay_outstanding=4, connection_outstanding=4)
    manager, context = _ready(limits=limits)
    relay = manager.bind_relay(context)
    ids: list[str] = []

    with pytest.raises(TypeError):
        manager.admit_operation(relay, {"n": "external"}, operation_id="external-0")  # type: ignore[call-arg]

    for n in range(300):
        operation = manager.admit_operation(relay, {"n": n})
        ids.append(operation.operation_id)
        _write(manager, relay)
        assert manager.accept_operation_response(
            relay,
            operation_id=operation.operation_id,
            payload={"n": n},
            **_fence(relay),
        )

    assert len(ids) == len(set(ids)) == 300
    assert [int(operation_id.rsplit(":", 1)[1]) for operation_id in ids] == list(range(1, 301))
    successor = manager.admit_operation(relay, {"n": "successor"})
    assert manager.accept_operation_response(
        relay,
        operation_id=ids[0],
        payload=ExplodingPayload(),
        **_fence(relay),
    ) is False
    assert successor.state == "admitted"
    assert successor.operation_id not in ids


def test_duplicate_response_cannot_change_frozen_terminal_outcome():
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context)
    operation = manager.admit_operation(relay, {"method": "Runtime.evaluate"})
    _write(manager, relay)
    assert manager.accept_operation_response(
        relay,
        operation_id=operation.operation_id,
        payload={"result": {"value": 1}},
        **_fence(relay),
    )
    frozen = operation.outcome
    assert manager.accept_operation_response(
        relay,
        operation_id=operation.operation_id,
        payload=ExplodingPayload(),
        **_fence(relay),
    ) is False
    assert operation.outcome == frozen


@pytest.mark.parametrize("malformed_id", [[], {}, True, 1.5, None, "", "x" * 129])
def test_malformed_operation_ids_are_typed_and_close_without_payload_access(malformed_id):
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context)
    operation = manager.admit_operation(relay, {"small": True})
    _write(manager, relay)

    with pytest.raises(BrowserProtocolError) as exc:
        manager.accept_operation_response(
            relay,
            operation_id=malformed_id,
            payload=ExplodingPayload(),
            **_fence(relay),
        )
    assert exc.value.code == "browser_invalid_operation_id"
    assert exc.value.outcome()["delivery"] == "outcome_unknown"
    assert operation.outcome["status"] == "browser_invalid_operation_id"
    assert operation.outcome["delivery"] == "outcome_unknown"
    assert relay.state == "closed"
    assert relay.outstanding_count == 0


def test_owner_fence_precedes_malformed_id_and_payload_access():
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context)
    operation = manager.admit_operation(relay, {"small": True})
    _write(manager, relay)
    attempted = _fence(relay)
    attempted["transport"] = object()

    with pytest.raises(BrowserProtocolError) as exc:
        manager.accept_operation_response(
            relay,
            operation_id=[],
            payload=ExplodingPayload(),
            **attempted,
        )
    assert exc.value.code == "browser_fence_transport"
    assert operation.state == "dispatched"
    assert relay.state == "active"


def test_malformed_failed_flag_is_typed_and_terminal():
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context)
    operation = manager.admit_operation(relay, {"small": True})
    _write(manager, relay)
    with pytest.raises(BrowserProtocolError) as exc:
        manager.accept_operation_response(
            relay,
            operation_id=operation.operation_id,
            payload={"result": 1},
            failed="false",  # type: ignore[arg-type]
            **_fence(relay),
        )
    assert exc.value.code == "browser_invalid_frame"
    assert operation.outcome["status"] == "browser_invalid_frame"
    assert operation.outcome["delivery"] == "outcome_unknown"
    assert relay.state == "closed"


def test_over_cap_response_is_typed_and_deterministically_closes_operation_and_relay():
    limits = _small_limits(application_message_bytes=40, queue_bytes=80, teardown_bytes=10)
    manager, context = _ready(limits=limits)
    relay = manager.bind_relay(context)
    with pytest.raises(BrowserProtocolError) as outbound_exc:
        manager.admit_operation(relay, {"secret": "x" * 100})
    assert outbound_exc.value.code == "browser_frame_too_large"
    assert relay.outstanding_count == 0

    operation = manager.admit_operation(relay, {"small": True})
    _write(manager, relay)
    with pytest.raises(BrowserProtocolError) as inbound_exc:
        manager.accept_operation_response(
            relay,
            operation_id=operation.operation_id,
            payload={"secret": "y" * 100},
            **_fence(relay),
        )
    assert inbound_exc.value.code == "browser_frame_too_large"
    assert inbound_exc.value.outcome()["delivery"] == "outcome_unknown"
    assert operation.outcome["status"] == "browser_frame_too_large"
    assert operation.outcome["delivery"] == "outcome_unknown"
    assert relay.outstanding_count == 0
    assert relay.state == "closed"


def test_rehello_closes_old_relays_settles_operations_and_replays_nothing():
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context, target_id="stable-surface-tab")
    old_token = relay.relay_token
    old_sid = relay.sid
    old_generation = relay.capability_generation
    dispatched = manager.admit_operation(relay, {"seq": 1})
    admitted = manager.admit_operation(relay, {"seq": 2})
    _write(manager, relay)

    outcome = manager.negotiate(context, _hello(context.connection_id), server_enabled=True)
    assert outcome["sid"] != old_sid
    assert outcome["capability_generation"] > old_generation
    assert relay.state == "closed"
    assert relay.outbound.message_count == 0
    assert admitted.outcome["delivery"] == "not_started"
    assert dispatched.outcome["delivery"] == "outcome_unknown"
    assert manager.live_relay_token_count == 0

    replacement = manager.bind_relay(context, target_id="stable-surface-tab")
    assert replacement.relay_token != old_token
    assert replacement.sid != old_sid
    assert replacement.outstanding_count == 0
    assert replacement.outbound.get() is None
    with pytest.raises(BrowserProtocolError) as stale:
        manager.authorize_frame(relay, **_fence(relay))
    assert stale.value.code == "browser_fence_sid"


def test_relay_token_uniqueness_state_returns_to_baseline_under_high_churn():
    manager, context = _ready(limits=_small_limits())
    baseline = manager.live_relay_token_count
    stale_relays = []
    for _ in range(300):
        relay = manager.bind_relay(context)
        stale_relays.append(relay)
        assert manager.live_relay_token_count == baseline + 1
        assert manager.close_relay(relay, status="browser_disconnected") is True
        assert manager.live_relay_token_count == baseline
        assert len(context.relays) == 0

    assert len({relay.relay_token for relay in stale_relays}) == 300
    with pytest.raises(BrowserProtocolError) as stale:
        manager.authorize_frame(stale_relays[0], **_fence(stale_relays[0]))
    assert stale.value.code == "browser_stale_binding_generation"


def test_one_target_binding_and_diagnostics_never_expose_identity_or_payload():
    manager, context = _ready(limits=_small_limits())
    target = "sensitive-target-id"
    relay = manager.bind_relay(context, target_id=target)
    secret = "https://user:password@example.test/?token=secret-value"
    manager.admit_operation(relay, {"url": secret})
    assert relay.target_id == target

    rendered = json.dumps(manager.redacted_diagnostics(), sort_keys=True)
    forbidden = (
        target,
        secret,
        relay.relay_token,
        relay.sid,
        relay.profile,
        context.principal,
        context.connection_id,
    )
    assert all(value not in rendered for value in forbidden)
    assert "target_bound" in rendered
    assert "outstanding" in rendered
    assert secret not in repr(relay)
    assert context.principal not in repr(context)
    assert context.connection_id not in repr(context)
    assert secret not in repr(relay.outbound.get())


def test_immediate_transport_close_is_idempotent_and_never_affects_chat_association():
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context)
    operation = manager.admit_operation(relay, {"method": "Page.navigate"})
    _write(manager, relay)
    associations = manager.association_count

    assert manager.close_relay(relay, status="browser_crashed") is True
    assert manager.close_relay(relay, status="browser_crashed") is False
    assert operation.outcome["status"] == "browser_crashed"
    assert operation.outcome["delivery"] == "outcome_unknown"
    assert manager.association_count == associations
    assert context.state == "ready"
    assert manager.live_relay_token_count == 0


def test_gateway_owned_monotonic_deadlines_classify_and_expire_without_wall_clock():
    now = [100.0]
    manager, context = _ready(limits=_small_limits(), monotonic=lambda: now[0])
    ordinary = manager.bind_relay(
        context, task_id="ordinary", tab_id="ordinary", target_id="ordinary"
    )
    navigation = manager.bind_relay(
        context, task_id="navigation", tab_id="navigation", target_id="navigation"
    )

    ordinary_operation = manager.admit_operation(
        ordinary, {"id": 1, "method": "Runtime.evaluate"}
    )
    navigation_operation = manager.admit_operation(
        navigation, {"id": 2, "method": "Page.navigate"}
    )
    promise_relay = manager.bind_relay(
        context, task_id="promise", tab_id="promise", target_id="promise"
    )
    promise_operation = manager.admit_operation(
        promise_relay,
        {
            "id": 3,
            "method": "Runtime.evaluate",
            "params": {"expression": "work()", "awaitPromise": True},
        },
    )
    assert ordinary_operation.timeout_seconds == 30.0
    assert navigation_operation.timeout_seconds == 60.0
    assert promise_operation.timeout_seconds == 60.0

    ordinary_frame = ordinary.outbound.get()
    assert ordinary_frame is not None
    envelope = manager.outbound_envelope(ordinary, ordinary_frame)
    assert envelope["remaining_duration_ms"] == 30_000
    ordinary.outbound.put(
        ordinary_frame.decode(), operation_id=ordinary_frame.operation_id
    )

    now[0] = 130.0
    assert manager.due_operations(context) == (ordinary_operation,)
    writer = RecordingWriter()
    assert asyncio.run(manager.timeout_operation(ordinary_operation, writer=writer)) is True
    assert writer.frames == [{"type": "browser.relay.close"}]
    assert ordinary_operation.outcome["status"] == "browser_timed_out"
    assert ordinary_operation.outcome["delivery"] == "not_started"
    assert ordinary_operation.outcome["retryable"] is True
    assert navigation.state == "active"


def test_socketless_consumer_disconnect_queues_one_exact_teardown_and_preserves_chat():
    manager, context = _ready(limits=_small_limits())
    relay = manager.bind_relay(context)
    operation = manager.admit_operation(relay, {"id": 1, "method": "Runtime.evaluate"})
    duplex = manager.relay_duplex(relay)
    associations = manager.association_count

    assert duplex.consumer_disconnected() is True
    assert duplex.consumer_disconnected() is False
    assert relay.state == "closing"
    assert relay.outbound.control_count == 1
    assert operation.outcome["status"] == "browser_cancelled"
    assert operation.outcome["delivery"] == "not_started"

    writer = RecordingWriter()
    asyncio.run(manager.write_next(relay, writer))
    assert writer.frames == [{"type": "browser.relay.close"}]
    assert relay.state == "closed"
    assert manager.association_count == associations
