"""Exactly-once proactive transport edge owned by the running Poke gateway."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
from typing import Any, Mapping

from gateway.proactive_scheduler import ContactRoute, ProactiveOwnershipRegistry, ProactiveScheduler, SlotClaim

_ALLOWED = frozenset({("poke", "kosta-owner", "owner"), ("guest", "stephen-lucier", "guest")})


@dataclass(frozen=True)
class ProactiveTransportResult:
    state: str
    success: bool
    message_id: str | None = None
    retryable: bool = False
    partial: bool = False
    unknown: bool = False
    error_class: str = ""
    retry_at: float | None = None


class BlueBubblesProactiveDelivery:
    """Use one already-connected Poke adapter and never create/fallback a chat."""

    def __init__(self, adapter: Any, *, owner_profile: str = "poke",
                 ownership_registry: ProactiveOwnershipRegistry | None = None,
                 runner_id: str = "", adapter_id: str = "",
                 participant_identities: Mapping[tuple[str, str], frozenset[str]] | None = None,
                 scheduler: ProactiveScheduler | None = None) -> None:
        self.adapter = adapter
        self.owner_profile = owner_profile
        self.ownership_registry = ownership_registry
        self.runner_id = runner_id
        self.adapter_id = adapter_id
        self.participant_identities = dict(participant_identities or {})
        self.scheduler = scheduler

    async def prepare(self, *, route: ContactRoute, slot_id: str) -> tuple[str | None, ProactiveTransportResult | None]:
        """Traverse ownership and authenticated existing-DM checks before an attempt."""
        if self.owner_profile != "poke":
            raise ValueError("Poke is the sole proactive transport owner")
        now = __import__("time").time()
        if self.ownership_registry is None or not self.ownership_registry.validate_transport(
            self.runner_id, self.adapter_id, now=now
        ):
            if self.ownership_registry is not None:
                self.ownership_registry.open_circuit("transport_owner_invalid", now=now)
            raise RuntimeError("proactive transport owner lease is not current")
        if (route.profile_name, route.contact_id, route.principal) not in _ALLOWED:
            if self.ownership_registry is not None:
                self.ownership_registry.open_circuit("nonallowlisted_attempt", now=now)
            raise ValueError("non-allowlisted proactive route")
        if route.chat_type.lower() != "dm" or not route.chat_id:
            raise ValueError("proactive delivery requires an existing DM route")
        platform = getattr(getattr(self.adapter, "platform", None), "value", getattr(self.adapter, "platform", None))
        if str(platform).lower() != "bluebubbles":
            self.ownership_registry.open_circuit("adapter_invariant_failed", now=now)
            raise ValueError("live adapter is not BlueBubbles")
        resolver = getattr(self.adapter, "resolve_authenticated_existing_dm", None)
        if resolver is None:
            self.ownership_registry.open_circuit("adapter_auth_preflight_missing", now=now)
            raise RuntimeError("BlueBubbles authenticated-DM preflight unavailable")
        expected = self.participant_identities.get((route.profile_name, route.contact_id))
        if not expected:
            self.ownership_registry.open_circuit("participant_registry_missing", now=now)
            raise RuntimeError("operator contact registry has no approved participant identity")
        authenticated = await resolver(route.chat_id, expected)
        if not authenticated:
            self.ownership_registry.open_circuit("participant_auth_failed", now=now)
            return ProactiveTransportResult("failed", False, retryable=False, error_class="route_auth_failed")
        guid, fingerprint = authenticated
        if self.scheduler is None or not self.scheduler.bind_route_fingerprint(route, fingerprint):
            self.ownership_registry.open_circuit("route_fingerprint_mismatch", now=now)
            raise RuntimeError("authenticated proactive route fingerprint changed")
        if not self.ownership_registry.reserve_global_send(slot_id, now=now):
            status = self.ownership_registry.global_send_status(now=now)
            if status["circuit_state"] == "open":
                raise RuntimeError("global proactive circuit is open")
            return None, ProactiveTransportResult(
                "spacing_wait", False, retryable=True,
                error_class="global_spacing_contention", retry_at=float(status["available_at"]),
            )
        return str(guid), None

    async def deliver(self, *, route: ContactRoute, text: str, slot_id: str,
                      correlation_id: str, prepared_guid: str | None = None) -> ProactiveTransportResult:
        if prepared_guid is None:
            prepared_guid, refusal = await self.prepare(route=route, slot_id=slot_id)
            if refusal is not None:
                return refusal
        guid = str(prepared_guid)
        # Pass the resolved GUID so send() cannot enter its address/new-chat path.
        try:
            result = await self.adapter.send(
                str(guid), str(text), metadata={"proactive": True, "slot_id": slot_id,
                                                "correlation_id": correlation_id},
            )
        except BaseException as exc:
            self.ownership_registry.finish_global_send(slot_id, sent=False, now=__import__("time").time())
            # Interruption around await may have delivered remotely.
            return ProactiveTransportResult("delivery_unknown", False, unknown=True,
                                            error_class=type(exc).__name__)
        raw = result.raw_response if isinstance(getattr(result, "raw_response", None), Mapping) else {}
        partial = bool(raw.get("partial_delivery"))
        message_id = str(result.message_id) if getattr(result, "message_id", None) else None
        if bool(getattr(result, "success", False)) and message_id:
            self.ownership_registry.finish_global_send(slot_id, sent=True, now=__import__("time").time())
            return ProactiveTransportResult("sent", True, message_id=message_id)
        self.ownership_registry.finish_global_send(slot_id, sent=False, now=__import__("time").time())
        if partial:
            return ProactiveTransportResult("partial_delivery", False, message_id=message_id,
                                            partial=True, error_class="partial_delivery")
        if bool(getattr(result, "success", False)) or message_id:
            return ProactiveTransportResult("delivery_unknown", False, message_id=message_id,
                                            unknown=True, error_class="malformed_success")
        retryable = bool(getattr(result, "retryable", False))
        return ProactiveTransportResult("failed" if retryable else "delivery_unknown", False,
                                        retryable=retryable, unknown=not retryable,
                                        error_class=str(getattr(result, "error_kind", None) or
                                                        type(getattr(result, "error", None)).__name__ or "send_error"))


async def deliver_prepared_exactly_once(
    *, scheduler: ProactiveScheduler, delivery: BlueBubblesProactiveDelivery,
    route: ContactRoute, claim: SlotClaim, text: str, correlation_id: str,
    now: float | None = None,
) -> str:
    """Last-moment validation, one durable attempt, normalized final accounting."""
    reservation = await asyncio.to_thread(scheduler.reserve_delivery, claim, text, now=now)
    text = str(reservation.get("prepared_payload") or "")
    if not text:
        return await asyncio.to_thread(
            scheduler.finish_delivery, claim, state="failed", reason="prepared_payload_missing", now=now
        )
    refusal = await asyncio.to_thread(scheduler.final_delivery_check, route, claim, now=now)
    if refusal:
        return await asyncio.to_thread(scheduler.finish_delivery, claim, state="suppressed", reason=refusal, now=now)
    if not await asyncio.to_thread(scheduler.begin_delivery_preflight, claim, now=now):
        row = await asyncio.to_thread(scheduler.get_slot, claim.slot_id)
        return str((row or {}).get("reason") or "attempt_not_available")
    try:
        prepared_guid, transport_refusal = await delivery.prepare(route=route, slot_id=claim.slot_id)
    except Exception as exc:
        return await asyncio.to_thread(
            scheduler.finish_delivery, claim, state="failed",
            reason=f"pre_send_invariant:{type(exc).__name__}", now=now,
        )
    if transport_refusal is not None:
        if transport_refusal.state == "spacing_wait":
            return await asyncio.to_thread(
                scheduler.defer_prepared_delivery, claim,
                not_before=float(transport_refusal.retry_at or (__import__("time").time() + 300)),
                reason=transport_refusal.error_class, now=now,
            )
        return await asyncio.to_thread(
            scheduler.finish_delivery, claim, state=transport_refusal.state,
            reason=transport_refusal.error_class or transport_refusal.state,
            message_id=transport_refusal.message_id, now=now,
        )
    # Authentication above is asynchronous. Re-authorize after it, then take
    # a SQLite write fence immediately before adapter.send. The ingress path
    # writes its observed sequence to the same DB, so arrival-vs-send has one
    # durable order instead of a check/use gap.
    refusal = await asyncio.to_thread(scheduler.final_delivery_check, route, claim, now=now)
    if refusal:
        delivery.ownership_registry.finish_global_send(
            claim.slot_id, sent=False, now=__import__("time").time()
        )
        return await asyncio.to_thread(
            scheduler.finish_delivery, claim, state="suppressed", reason=refusal, now=now,
        )
    fence, fence_refusal = scheduler.begin_atomic_send_fence(claim, now=now)
    if fence is None:
        delivery.ownership_registry.finish_global_send(
            claim.slot_id, sent=False, now=__import__("time").time()
        )
        return await asyncio.to_thread(
            scheduler.finish_delivery, claim, state="suppressed",
            reason=str(fence_refusal or "send_fence_refused"), now=now,
        )
    try:
        try:
            result = await delivery.deliver(route=route, text=text, slot_id=claim.slot_id,
                                            correlation_id=correlation_id, prepared_guid=prepared_guid)
        finally:
            scheduler.finish_atomic_send_fence(fence)
    except Exception as exc:
        return await asyncio.to_thread(
            scheduler.finish_delivery, claim, state="failed",
            reason=f"pre_send_invariant:{type(exc).__name__}", now=now,
        )
    return await asyncio.to_thread(
        scheduler.finish_delivery, claim, state=result.state,
        reason=result.error_class or result.state, message_id=result.message_id,
        retryable=result.retryable and not result.partial, now=now,
    )


__all__ = ["BlueBubblesProactiveDelivery", "ProactiveTransportResult", "deliver_prepared_exactly_once"]
