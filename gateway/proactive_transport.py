"""Exactly-once proactive transport edge owned by the running Poke gateway."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
from typing import Any, Mapping

from gateway.proactive_scheduler import ContactRoute, ProactiveScheduler, SlotClaim

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


class BlueBubblesProactiveDelivery:
    """Use one already-connected Poke adapter and never create/fallback a chat."""

    def __init__(self, adapter: Any, *, owner_profile: str = "poke") -> None:
        self.adapter = adapter
        self.owner_profile = owner_profile

    async def deliver(self, *, route: ContactRoute, text: str, slot_id: str,
                      correlation_id: str) -> ProactiveTransportResult:
        if self.owner_profile != "poke":
            raise ValueError("Poke is the sole proactive transport owner")
        if (route.profile_name, route.contact_id, route.principal) not in _ALLOWED:
            raise ValueError("non-allowlisted proactive route")
        if route.chat_type.lower() != "dm" or not route.chat_id:
            raise ValueError("proactive delivery requires an existing DM route")
        platform = getattr(getattr(self.adapter, "platform", None), "value", getattr(self.adapter, "platform", None))
        if str(platform).lower() != "bluebubbles":
            raise ValueError("live adapter is not BlueBubbles")
        resolver = getattr(self.adapter, "_resolve_chat_guid", None)
        if resolver is None:
            raise RuntimeError("BlueBubbles existing-chat preflight unavailable")
        guid = await resolver(route.chat_id)
        if not guid:
            return ProactiveTransportResult("failed", False, retryable=False, error_class="chat_not_found")
        # Pass the resolved GUID so send() cannot enter its address/new-chat path.
        try:
            result = await self.adapter.send(
                str(guid), str(text), metadata={"proactive": True, "slot_id": slot_id,
                                                "correlation_id": correlation_id},
            )
        except BaseException as exc:
            # Interruption around await may have delivered remotely.
            return ProactiveTransportResult("delivery_unknown", False, unknown=True,
                                            error_class=type(exc).__name__)
        raw = result.raw_response if isinstance(getattr(result, "raw_response", None), Mapping) else {}
        partial = bool(raw.get("partial_delivery"))
        message_id = str(result.message_id) if getattr(result, "message_id", None) else None
        if bool(getattr(result, "success", False)) and message_id:
            return ProactiveTransportResult("sent", True, message_id=message_id)
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
    await asyncio.to_thread(scheduler.reserve_delivery, claim, text, now=now)
    refusal = await asyncio.to_thread(scheduler.final_delivery_check, route, claim, now=now)
    if refusal:
        return await asyncio.to_thread(scheduler.finish_delivery, claim, state="suppressed", reason=refusal, now=now)
    if not await asyncio.to_thread(scheduler.begin_delivery_attempt, claim, now=now):
        row = scheduler.get_slot(claim.slot_id)
        return str((row or {}).get("reason") or "attempt_not_available")
    result = await delivery.deliver(route=route, text=text, slot_id=claim.slot_id,
                                    correlation_id=correlation_id)
    return await asyncio.to_thread(
        scheduler.finish_delivery, claim, state=result.state,
        reason=result.error_class or result.state, message_id=result.message_id,
        retryable=result.retryable and not result.partial, now=now,
    )


__all__ = ["BlueBubblesProactiveDelivery", "ProactiveTransportResult", "deliver_prepared_exactly_once"]
