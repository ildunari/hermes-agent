"""Tests for the capability-gated authenticated existing-DM platform action.

Safety contract under test: no chat creation, no fallback target, tri-state
outcome, reservation-key idempotency propagation, and never a duplicate send.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.authenticated_dm import classify_send_error, send_authenticated_existing_dm
from gateway.conversation_extensions import (
    AuthenticatedDmRequest,
    AuthenticatedDmResult,
    DmSendOutcome,
    GatewayHostOperations,
    GatewayRuntimeFacade,
    CapabilityDenied,
)


def _request(**kwargs) -> AuthenticatedDmRequest:
    base = dict(platform="telegram", chat_id="12345", text="hello", reservation_key="r-1")
    base.update(kwargs)
    return AuthenticatedDmRequest(**base)


_UNSET = object()


class _Adapter:
    def __init__(self, result=_UNSET, raises: Exception | None = None):
        self.result = {"ok": True, "message_id": "m-1"} if result is _UNSET else result
        self.raises = raises
        self.calls: list[tuple] = []

    def send(self, chat_id, text, metadata=None):
        self.calls.append((chat_id, text, metadata))
        if self.raises is not None:
            raise self.raises
        return self.result


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_sends_to_authorized_existing_dm():
    adapter = _Adapter()
    result = send_authenticated_existing_dm(
        _request(),
        resolve_transport=lambda platform: adapter,
        is_authorized_existing_dm=lambda platform, chat_id: True,
    )
    assert result.outcome is DmSendOutcome.SENT
    assert result.receipt == "m-1"
    assert adapter.calls == [("12345", "hello", {"idempotency_key": "r-1"})]


def test_reservation_key_is_propagated_as_idempotency_key():
    """A retry after UNKNOWN must be de-duplicable at the transport."""
    adapter = _Adapter()
    send_authenticated_existing_dm(
        _request(reservation_key="attempt-42"),
        resolve_transport=lambda platform: adapter,
        is_authorized_existing_dm=lambda platform, chat_id: True,
    )
    assert adapter.calls[0][2]["idempotency_key"] == "attempt-42"


# ---------------------------------------------------------------------------
# no creation / no fallback
# ---------------------------------------------------------------------------


def test_unauthorized_target_fails_definitively_without_sending():
    adapter = _Adapter()
    result = send_authenticated_existing_dm(
        _request(),
        resolve_transport=lambda platform: adapter,
        is_authorized_existing_dm=lambda platform, chat_id: False,
    )
    assert result.outcome is DmSendOutcome.DEFINITIVE_FAILURE
    assert result.detail == "not_an_authorized_existing_dm"
    assert adapter.calls == []


def test_missing_transport_never_falls_back_to_another_platform():
    other = _Adapter()

    def _resolve(platform):
        # Only 'discord' has a transport; the request names 'telegram'.
        return other if platform == "discord" else None

    result = send_authenticated_existing_dm(
        _request(platform="telegram"),
        resolve_transport=_resolve,
        is_authorized_existing_dm=lambda platform, chat_id: True,
    )
    assert result.outcome is DmSendOutcome.DEFINITIVE_FAILURE
    assert result.detail == "transport_unavailable"
    assert other.calls == []


def test_request_has_no_chat_creation_affordance():
    request = _request()
    assert not hasattr(request, "create_if_missing")
    assert not hasattr(request, "fallback_platform")
    assert not hasattr(request, "recipient_lookup")


def test_authorization_probe_error_fails_closed_without_sending():
    adapter = _Adapter()

    def _boom(platform, chat_id):
        raise RuntimeError("registry down")

    result = send_authenticated_existing_dm(
        _request(),
        resolve_transport=lambda platform: adapter,
        is_authorized_existing_dm=_boom,
    )
    assert result.outcome is DmSendOutcome.DEFINITIVE_FAILURE
    assert adapter.calls == []


# ---------------------------------------------------------------------------
# tri-state classification
# ---------------------------------------------------------------------------


def test_definitive_transport_error_is_definitive():
    adapter = _Adapter(result={"ok": False, "error": "Bad Request: chat not found"})
    result = send_authenticated_existing_dm(
        _request(),
        resolve_transport=lambda platform: adapter,
        is_authorized_existing_dm=lambda platform, chat_id: True,
    )
    assert result.outcome is DmSendOutcome.DEFINITIVE_FAILURE


def test_timeout_is_unknown_not_failure():
    """The duplicate-send bug class: an ambiguous timeout must stay UNKNOWN."""
    adapter = _Adapter(raises=TimeoutError("request timed out"))
    result = send_authenticated_existing_dm(
        _request(),
        resolve_transport=lambda platform: adapter,
        is_authorized_existing_dm=lambda platform, chat_id: True,
    )
    assert result.outcome is DmSendOutcome.UNKNOWN


def test_unrecognized_error_defaults_to_unknown():
    adapter = _Adapter(result={"ok": False, "error": "something we have never seen"})
    result = send_authenticated_existing_dm(
        _request(),
        resolve_transport=lambda platform: adapter,
        is_authorized_existing_dm=lambda platform, chat_id: True,
    )
    assert result.outcome is DmSendOutcome.UNKNOWN


def test_indeterminate_result_is_unknown():
    adapter = _Adapter(result=None)
    result = send_authenticated_existing_dm(
        _request(),
        resolve_transport=lambda platform: adapter,
        is_authorized_existing_dm=lambda platform, chat_id: True,
    )
    assert result.outcome is DmSendOutcome.UNKNOWN


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Forbidden: bot was blocked by the user", DmSendOutcome.DEFINITIVE_FAILURE),
        ("chat_not_found", DmSendOutcome.DEFINITIVE_FAILURE),
        ("connection reset by peer", DmSendOutcome.UNKNOWN),
        ("504 gateway timeout", DmSendOutcome.UNKNOWN),
        ("", DmSendOutcome.UNKNOWN),
        (None, DmSendOutcome.UNKNOWN),
    ],
)
def test_classify_send_error(text, expected):
    assert classify_send_error(text) is expected


# ---------------------------------------------------------------------------
# async transport bridging
# ---------------------------------------------------------------------------


def test_async_adapter_requires_an_explicit_loop_bridge():
    class _AsyncAdapter:
        def __init__(self):
            self.calls = []

        async def send(self, chat_id, text, metadata=None):
            self.calls.append((chat_id, text, metadata))
            return {"ok": True, "message_id": "m-async"}

    adapter = _AsyncAdapter()
    result = send_authenticated_existing_dm(
        _request(),
        resolve_transport=lambda platform: adapter,
        is_authorized_existing_dm=lambda platform, chat_id: True,
    )
    assert result.outcome is DmSendOutcome.DEFINITIVE_FAILURE
    assert result.detail == "no_event_loop_bridge"
    assert adapter.calls == []


def test_async_adapter_sends_with_loop_bridge():
    class _AsyncAdapter:
        async def send(self, chat_id, text, metadata=None):
            return {"ok": True, "message_id": "m-async"}

    result = send_authenticated_existing_dm(
        _request(),
        resolve_transport=lambda platform: _AsyncAdapter(),
        is_authorized_existing_dm=lambda platform, chat_id: True,
        run_coroutine=asyncio.run,
    )
    assert result.outcome is DmSendOutcome.SENT
    assert result.receipt == "m-async"


# ---------------------------------------------------------------------------
# capability gating through the runtime facade
# ---------------------------------------------------------------------------


def test_facade_denies_dm_send_without_capability():
    facade = GatewayRuntimeFacade(
        extension_id="ext",
        profile_name="root",
        profile_home="/home/a",
        generation=1,
        capabilities=frozenset({"tool_authorization"}),
        host=GatewayHostOperations(),
    )
    with pytest.raises(CapabilityDenied):
        facade.send_authenticated_existing_dm(_request())


def test_facade_allows_dm_send_with_capability():
    sent: list[AuthenticatedDmRequest] = []

    def _send(request):
        sent.append(request)
        return AuthenticatedDmResult(DmSendOutcome.SENT, receipt="m-1")

    facade = GatewayRuntimeFacade(
        extension_id="ext",
        profile_name="root",
        profile_home="/home/a",
        generation=1,
        capabilities=frozenset({"authenticated_dm"}),
        host=GatewayHostOperations(send_authenticated_existing_dm=_send),
    )
    result = facade.send_authenticated_existing_dm(_request())
    assert result.outcome is DmSendOutcome.SENT
    assert len(sent) == 1


def test_facade_without_host_implementation_fails_definitively():
    facade = GatewayRuntimeFacade(
        extension_id="ext",
        profile_name="root",
        profile_home="/home/a",
        generation=1,
        capabilities=frozenset({"authenticated_dm"}),
        host=GatewayHostOperations(),
    )
    result = facade.send_authenticated_existing_dm(_request())
    assert result.outcome is DmSendOutcome.DEFINITIVE_FAILURE
    assert result.detail == "capability_unavailable"


def test_facade_does_not_expose_prepared_delivery():
    """Plan decision 2: no prepared-delivery runtime facade."""
    facade = GatewayRuntimeFacade(
        extension_id="ext",
        profile_name="root",
        profile_home="/home/a",
        generation=1,
        capabilities=frozenset({"authenticated_dm"}),
        host=GatewayHostOperations(),
    )
    public = {name for name in dir(facade) if not name.startswith("_")}
    assert "prepare_delivery" not in public
    assert "delivery_router" not in public
    assert "send_message" not in public
