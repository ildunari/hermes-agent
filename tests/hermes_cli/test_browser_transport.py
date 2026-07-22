"""Behavior-first contract tests for the Phase-1 dark browser transport."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets

import pytest

from hermes_cli.browser_transport import (
    BrowserProtocolError,
    BrowserTransportManager,
    WIRE_CONTRACT,
    browser_sid_is_non_resumable,
    method_set_hash,
)
from hermes_cli.dashboard_auth import ws_tickets


def _connection_id() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: list[tuple[int, str]] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


def _hello(connection_id: str, profile: str = "gpt", **overrides) -> dict:
    hello = {
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
    hello["browser"].update(overrides)
    return hello


def _ready(manager: BrowserTransportManager, *, principal: str = "nous:u1", profile: str = "gpt"):
    connection_id = _connection_id()
    manager.register_chat(
        transport=object(), principal=principal, profile=profile, connection_id=connection_id
    )
    socket = FakeSocket()
    context = manager.new_browser_context(
        transport=socket,
        ticket={"principal": principal, "profile": profile, "connection_id": connection_id},
    )
    outcome = manager.negotiate(context, _hello(connection_id, profile), server_enabled=True)
    assert outcome["status"] == "ready"
    return context, outcome


def test_method_set_hash_is_order_independent_and_direction_sensitive():
    rows = list(reversed(WIRE_CONTRACT["required_methods"]))
    assert method_set_hash(rows) == method_set_hash()
    mutated = [dict(row) for row in rows]
    mutated[0]["direction"] = "wrong-way"
    assert method_set_hash(mutated) != method_set_hash()


def test_association_requires_256_bits_and_is_memory_only():
    manager = BrowserTransportManager()
    ids = {_connection_id() for _ in range(256)}
    assert len(ids) == 256


    for connection_id in ids:
        assert manager.validate_connection_id(connection_id) == connection_id
    # Property-style boundary sweep: canonical base64url accepts exactly 32
    # decoded bytes and no adjacent or oversized length.
    for size in range(65):
        candidate = base64.urlsafe_b64encode(bytes(size)).rstrip(b"=").decode()
        if size == 32:
            assert manager.validate_connection_id(candidate) == candidate
        else:
            with pytest.raises(BrowserProtocolError, match="connection_id"):
                manager.validate_connection_id(candidate)

    manager.register_chat(transport=object(), principal="p", profile="gpt", connection_id=next(iter(ids)))
    assert manager.association_count == 1
    # A process/Desktop restart constructs a fresh manager; no association is persisted.
    assert BrowserTransportManager().association_count == 0


def test_browser_sid_is_non_resumable_while_active_and_during_bounded_tombstone():
    manager = BrowserTransportManager()
    context, outcome = _ready(manager)
    sid = outcome["sid"]

    assert browser_sid_is_non_resumable(sid)
    manager.disconnect(context)
    assert browser_sid_is_non_resumable(sid)

    # Expired entries are pruned on lookup and no longer reserve chat names.
    manager._retired_sids[sid] = 0.0
    assert not browser_sid_is_non_resumable(sid)


def test_upload_scope_requires_distinct_server_transport_and_exact_live_task():
    manager = BrowserTransportManager()
    context, outcome = _ready(manager)
    manager.bind_relay(context, task_id="task-1", tab_id="tab-1", guest_generation="guest-1")
    exact = {
        "principal": context.principal,
        "profile": context.profile,
        "connection_id": context.connection_id,
        "transport_id": outcome["transport_id"],
        "browser_sid": outcome["sid"],
        "capability_generation": str(outcome["capability_generation"]),
        "binding_generation": str(outcome["binding_generation"]),
        "task_id": "task-1",
        "task_generation": "1",
        "tab_id": "tab-1",
        "source_session_id": "task-1",
    }

    assert outcome["transport_id"] != outcome["sid"]
    manager.validate_upload_scope(**exact)
    with pytest.raises(BrowserProtocolError, match="upload browser lease is stale"):
        manager.validate_upload_scope(**{**exact, "transport_id": outcome["sid"]})
    with pytest.raises(BrowserProtocolError, match="upload task is not bound"):
        manager.validate_upload_scope(**{**exact, "task_id": "task-2"})
    with pytest.raises(BrowserProtocolError, match="upload task is not bound"):
        manager.validate_upload_scope(**{**exact, "task_generation": "2"})
    with pytest.raises(BrowserProtocolError, match="upload task is not bound"):
        manager.validate_upload_scope(**{**exact, "source_session_id": "other-session"})


def test_browser_first_waits_for_independently_authenticated_chat_without_timing_delay():
    async def exercise():
        manager = BrowserTransportManager()
        connection_id = _connection_id()
        context = manager.new_browser_context(
            transport=FakeSocket(),
            ticket={"principal": "nous:u1", "profile": "gpt", "connection_id": connection_id},
        )
        waiter = asyncio.create_task(manager.wait_for_chat(context, timeout=0.5))
        await asyncio.sleep(0)
        assert not waiter.done()
        manager.register_chat(
            transport=object(),
            principal="nous:u1",
            profile="gpt",
            connection_id=connection_id,
        )
        await waiter

    asyncio.run(exercise())


def test_wrong_principal_and_wrong_profile_fail_before_capability():
    manager = BrowserTransportManager()
    connection_id = _connection_id()
    manager.register_chat(transport=object(), principal="nous:owner", profile="gpt", connection_id=connection_id)

    wrong_principal = manager.new_browser_context(
        transport=FakeSocket(),
        ticket={"principal": "nous:attacker", "profile": "gpt", "connection_id": connection_id},
    )
    with pytest.raises(BrowserProtocolError) as exc:
        manager.negotiate(wrong_principal, _hello(connection_id), server_enabled=True)
    assert exc.value.code == "browser_wrong_principal"

    wrong_profile = manager.new_browser_context(
        transport=FakeSocket(),
        ticket={"principal": "nous:owner", "profile": "gpt", "connection_id": connection_id},
    )
    with pytest.raises(BrowserProtocolError) as exc:
        manager.negotiate(wrong_profile, _hello(connection_id, "default"), server_enabled=True)
    assert exc.value.code == "browser_wrong_profile"
    assert manager.ready_count == 0


def test_disabled_and_incompatible_hello_are_typed_and_fail_dark():
    manager = BrowserTransportManager()
    connection_id = _connection_id()
    manager.register_chat(transport=object(), principal="p", profile="gpt", connection_id=connection_id)

    disabled = manager.new_browser_context(
        transport=FakeSocket(), ticket={"principal": "p", "profile": "gpt", "connection_id": connection_id}
    )
    result = manager.negotiate(disabled, _hello(connection_id), server_enabled=False)
    assert result["status"] == "browser_disabled"
    assert result["retryable"] is False
    assert disabled.present is False

    incompatible = manager.new_browser_context(
        transport=FakeSocket(), ticket={"principal": "p", "profile": "gpt", "connection_id": connection_id}
    )
    result = manager.negotiate(
        incompatible,
        _hello(connection_id, method_set_hash="0" * 64),
        server_enabled=True,
    )
    assert result["status"] == "browser_incompatible"
    assert result["local"]["method_set_hash"] == method_set_hash()
    assert incompatible.present is False


def test_capability_and_binding_generations_are_monotonic_and_stale_values_fail():
    manager = BrowserTransportManager()
    context, first = _ready(manager)
    relay = manager.bind_relay(context)
    assert relay.binding_generation == first["binding_generation"]

    manager.bump_binding_generation(context)
    with pytest.raises(BrowserProtocolError) as exc:
        manager.authorize_frame(
            relay,
            transport=context.transport,
            sid=relay.sid,
            profile=relay.profile,
            capability_generation=relay.capability_generation,
            relay_token=relay.relay_token,
            binding_generation=relay.binding_generation,
        )
    assert exc.value.code == "browser_stale_binding_generation"

    second = manager.negotiate(context, _hello(context.connection_id), server_enabled=True)
    assert second["capability_generation"] > first["capability_generation"]
    assert second["binding_generation"] > first["binding_generation"]


def test_exact_five_field_fence_rejects_each_mismatch_and_accepts_exact_tuple():
    manager = BrowserTransportManager()
    context, _ = _ready(manager)
    relay = manager.bind_relay(context)
    good = {
        "transport": context.transport,
        "sid": relay.sid,
        "profile": relay.profile,
        "capability_generation": relay.capability_generation,
        "relay_token": relay.relay_token,
        "binding_generation": relay.binding_generation,
    }
    manager.authorize_frame(relay, **good)

    cases = {
        "transport": object(),
        "sid": "wrong-sid",
        "profile": "default",
        "capability_generation": relay.capability_generation + 1,
        "relay_token": secrets.token_urlsafe(32),
    }
    for field, bad in cases.items():
        attempted = dict(good)
        attempted[field] = bad
        with pytest.raises(BrowserProtocolError) as exc:
            manager.authorize_frame(relay, **attempted)
        assert exc.value.code == f"browser_fence_{field}", field

    malformed_owner_fields = (
        ("sid", 1, "browser_fence_sid"),
        ("profile", True, "browser_fence_profile"),
        ("capability_generation", True, "browser_fence_capability_generation"),
        ("capability_generation", str(relay.capability_generation), "browser_fence_capability_generation"),
        ("capability_generation", float(relay.capability_generation), "browser_fence_capability_generation"),
        ("relay_token", True, "browser_fence_relay_token"),
        ("binding_generation", True, "browser_stale_binding_generation"),
        ("binding_generation", str(relay.binding_generation), "browser_stale_binding_generation"),
        ("binding_generation", float(relay.binding_generation), "browser_stale_binding_generation"),
    )
    for field, malformed, expected_code in malformed_owner_fields:
        attempted = dict(good)
        attempted[field] = malformed
        with pytest.raises(BrowserProtocolError) as exc:
            manager.authorize_frame(relay, **attempted)
        assert exc.value.code == expected_code, (field, malformed)


def test_disable_kill_and_revoke_close_browser_only():
    async def exercise():
        manager = BrowserTransportManager()
        first, _ = _ready(manager, principal="nous:u1")
        second, _ = _ready(manager, principal="nous:u2")
        chat_count = manager.association_count

        assert await manager.kill(principal="nous:attacker", profile="gpt") == 0
        assert not first.transport.closed
        assert not second.transport.closed

        assert await manager.revoke(principal="nous:u1", profile="gpt", reason="browser_revoked") == 1
        assert first.transport.sent[-1]["status"] == "browser_revoked"
        assert first.transport.closed
        assert not second.transport.closed
        assert manager.association_count == chat_count  # chat associations are untouched

        assert await manager.kill(principal="nous:u2", profile="gpt") == 1
        assert second.transport.sent[-1]["status"] == "browser_killed"
        assert second.transport.closed
        assert manager.association_count == chat_count

    asyncio.run(exercise())


def test_browser_tickets_are_fresh_single_use_expiring_and_audience_separated(monkeypatch):
    ws_tickets._reset_for_tests()
    clock = {"now": 1_000_000}
    monkeypatch.setattr(ws_tickets.time, "time", lambda: clock["now"])
    connection_id = _connection_id()

    ticket = ws_tickets.mint_browser_ticket(
        user_id="u1", provider="nous", profile="gpt", connection_id=connection_id
    )
    info = ws_tickets.consume_browser_ticket(ticket)
    assert info == {
        "user_id": "u1",
        "provider": "nous",
        "profile": "gpt",
        "connection_id": connection_id,
        "minted_at": clock["now"],
        "audience": "browser",
    }
    with pytest.raises(ws_tickets.TicketInvalid):
        ws_tickets.consume_browser_ticket(ticket)

    generic = ws_tickets.mint_ticket(user_id="u1", provider="nous")
    with pytest.raises(ws_tickets.TicketInvalid, match="audience"):
        ws_tickets.consume_browser_ticket(generic)

    expired = ws_tickets.mint_browser_ticket(
        user_id="u1", provider="nous", profile="gpt", connection_id=connection_id
    )
    clock["now"] += ws_tickets.TTL_SECONDS
    with pytest.raises(ws_tickets.TicketInvalid, match="expired"):
        ws_tickets.consume_browser_ticket(expired)


def test_dark_transport_states_leave_exact_twelve_tool_schema_bytes_and_registry_frozen():
    """Capability state is runtime routing state, never model/prompt schema state."""

    import tools.browser_cdp_tool  # noqa: F401
    import tools.browser_dialog_tool  # noqa: F401
    import tools.browser_tool  # noqa: F401
    from tools.registry import registry

    names = (
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_back",
        "browser_press",
        "browser_get_images",
        "browser_vision",
        "browser_console",
        "browser_cdp",
        "browser_dialog",
    )
    entries = [registry.get_entry(name) for name in names]
    assert all(entry is not None for entry in entries)
    before_generation = registry._generation
    before = b"\n".join(
        json.dumps(entry.schema, ensure_ascii=False, separators=(",", ":")).encode()
        for entry in entries
        if entry is not None
    )

    manager = BrowserTransportManager()
    connection_id = _connection_id()
    manager.register_chat(
        transport=object(), principal="nous:u1", profile="gpt", connection_id=connection_id
    )
    disabled = manager.new_browser_context(
        transport=FakeSocket(),
        ticket={"principal": "nous:u1", "profile": "gpt", "connection_id": connection_id},
    )
    assert manager.negotiate(disabled, _hello(connection_id), server_enabled=False)["status"] == "browser_disabled"
    incompatible = manager.new_browser_context(
        transport=FakeSocket(),
        ticket={"principal": "nous:u1", "profile": "gpt", "connection_id": connection_id},
    )
    assert (
        manager.negotiate(
            incompatible,
            _hello(connection_id, method_set_hash="0" * 64),
            server_enabled=True,
        )["status"]
        == "browser_incompatible"
    )
    ready = manager.new_browser_context(
        transport=FakeSocket(),
        ticket={"principal": "nous:u1", "profile": "gpt", "connection_id": connection_id},
    )
    assert manager.negotiate(ready, _hello(connection_id), server_enabled=True)["status"] == "ready"
    manager.disconnect(ready)

    after = b"\n".join(
        json.dumps(entry.schema, ensure_ascii=False, separators=(",", ":")).encode()
        for entry in entries
        if entry is not None
    )
    assert len(names) == len(set(names)) == 12
    assert before == after
    assert registry._generation == before_generation


def test_operational_relay_resolution_requires_every_immutable_owner_fence():
    manager = BrowserTransportManager()
    context, _ = _ready(manager)
    chat_transport = manager.chat_transport_for(context)
    relay = manager.bind_relay(
        context,
        chat_transport=chat_transport,
        task_id="task-exact",
        tab_id="tab-exact",
        guest_generation="guest-exact",
        role="automation",
        task_generation=5,
    )
    exact = {
        "chat_transport": chat_transport,
        "task_id": "task-exact",
        "tab_id": "tab-exact",
        "guest_generation": "guest-exact",
        "role": "automation",
        "task_generation": 5,
        "relay_token": relay.relay_token,
    }

    assert manager.resolve_relay(context, **exact) is relay
    for field, stale in (
        ("chat_transport", object()),
        ("task_id", "task-other"),
        ("tab_id", "tab-other"),
        ("guest_generation", "guest-other"),
        ("role", "raw"),
        ("task_generation", 4),
        ("relay_token", "X" * 43),
    ):
        with pytest.raises(BrowserProtocolError, match="browser_relay_not_found"):
            manager.resolve_relay(context, **{**exact, field: stale})


def test_tool_adapter_rebinds_both_roles_and_stale_teardown_is_inert():
    manager = BrowserTransportManager()
    context, _ = _ready(manager)
    chat_transport = manager.chat_transport_for(context)
    first = manager.bind_tool_adapter(
        context,
        chat_transport=chat_transport,
        task_id="task-role-scope",
        tab_id="tab-role-scope",
        guest_generation="guest-1",
        task_generation=1,
    )

    try:
        assert manager.bind_tool_adapter(
            context,
            chat_transport=chat_transport,
            task_id="task-role-scope",
            tab_id="tab-role-scope",
            guest_generation="guest-1",
            task_generation=1,
        ) is first

        second = manager.bind_tool_adapter(
            context,
            chat_transport=chat_transport,
            task_id="task-role-scope",
            tab_id="tab-role-scope",
            guest_generation="guest-2",
            task_generation=2,
        )
        assert first.closed is True
        assert {relay.role for relay in context.relays if relay.active} == {
            "automation",
            "raw-cdp",
        }
        assert manager.unbind_tool_adapter(
            context,
            task_id="task-role-scope",
            tab_id="tab-role-scope",
            guest_generation="guest-1",
            task_generation=1,
        ) is False
        assert second.closed is False
        assert manager.unbind_tool_adapter(
            context,
            task_id="task-role-scope",
            tab_id="tab-role-scope",
            guest_generation="guest-2",
            task_generation=2,
        ) is True
        with pytest.raises(BrowserProtocolError, match="browser_stale_task_generation"):
            manager.bind_tool_adapter(
                context,
                chat_transport=chat_transport,
                task_id="task-role-scope",
                tab_id="tab-role-scope",
                guest_generation="guest-2",
                task_generation=2,
            )
    finally:
        manager.close_context_adapters(context)
