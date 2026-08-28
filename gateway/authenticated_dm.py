"""Capability-gated authenticated existing-DM platform action.

This is the *only* outbound send surface a conversation extension may reach,
and it is deliberately the narrowest one that can support exactly-once
proactive delivery:

* **No chat creation.** The target must already exist and already be
  authorized for the transport. There is no ``create_if_missing``.
* **No fallback.** If the named platform's transport is unavailable, the call
  fails definitively rather than silently rerouting to another platform, chat,
  or relay.
* **Tri-state result.** ``sent`` / ``definitive_failure`` / ``unknown``. The
  ``unknown`` state exists because a transport timeout is genuinely ambiguous;
  a caller must never blind-retry it. Retry/ledger ownership stays with the
  extension, not with core.
* **Reservation first.** The caller must durably reserve
  ``reservation_key`` *before* invoking this action, so a retry after
  ``unknown`` is idempotent at the transport layer.

Core deliberately does not expose a prepared-delivery facade: composing a
message and deciding to send it stay on the plugin side of the seam.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Mapping, Optional

from gateway.conversation_extensions import (
    AuthenticatedDmRequest,
    AuthenticatedDmResult,
    DmSendOutcome,
)

logger = logging.getLogger(__name__)


# Errors that mean "this will never succeed as addressed" rather than
# "the outcome is unclear". Everything not definitively classified is UNKNOWN,
# because treating an ambiguous timeout as a definitive failure is exactly how
# duplicate proactive sends happen.
_DEFINITIVE_ERROR_MARKERS = (
    "chat not found",
    "chat_not_found",
    "user not found",
    "user_not_found",
    "peer_id_invalid",
    "bot was blocked",
    "forbidden",
    "unauthorized",
    "not authorized",
    "invalid chat_id",
)

_UNKNOWN_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "connection reset",
    "temporarily unavailable",
    "503",
    "502",
    "504",
)


def classify_send_error(error_text: Optional[str]) -> DmSendOutcome:
    """Classify a transport error into the tri-state outcome.

    Unrecognized errors return ``UNKNOWN`` on purpose: an unknown outcome is
    preserved for operator review, while a wrong ``DEFINITIVE_FAILURE`` would
    invite a duplicate retry.
    """
    text = (error_text or "").strip().lower()
    if not text:
        return DmSendOutcome.UNKNOWN
    for marker in _DEFINITIVE_ERROR_MARKERS:
        if marker in text:
            return DmSendOutcome.DEFINITIVE_FAILURE
    for marker in _UNKNOWN_ERROR_MARKERS:
        if marker in text:
            return DmSendOutcome.UNKNOWN
    return DmSendOutcome.UNKNOWN


def _extract_receipt(result: Any) -> Optional[str]:
    if isinstance(result, Mapping):
        for key in ("message_id", "id", "receipt"):
            value = result.get(key)
            if value not in (None, ""):
                return str(value)
    message_id = getattr(result, "message_id", None)
    return str(message_id) if message_id not in (None, "") else None


def _result_error_text(result: Any) -> Optional[str]:
    if isinstance(result, Mapping):
        for key in ("error", "detail", "message"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        if result.get("ok") is False or result.get("success") is False:
            return "send reported failure"
        return None
    error = getattr(result, "error", None)
    if isinstance(error, str) and error.strip():
        return error
    success = getattr(result, "success", None)
    if success is False:
        return "send reported failure"
    return None


def _looks_successful(result: Any) -> bool:
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    if isinstance(result, Mapping):
        if result.get("ok") is True or result.get("success") is True:
            return True
        return _result_error_text(result) is None
    success = getattr(result, "success", None)
    if success is not None:
        return bool(success)
    return _result_error_text(result) is None


def send_authenticated_existing_dm(
    request: AuthenticatedDmRequest,
    *,
    resolve_transport: Callable[[str], Any],
    is_authorized_existing_dm: Callable[[str, str], bool],
    run_coroutine: Optional[Callable[[Any], Any]] = None,
) -> AuthenticatedDmResult:
    """Send to an existing, already-authorized DM.

    ``resolve_transport`` returns a live adapter for the named platform or
    ``None``; core never falls back to a different platform. Nothing here
    creates a chat, resolves a contact, or looks up an address book —
    ``chat_id`` must already be a real, authorized conversation.
    """
    if not isinstance(request, AuthenticatedDmRequest):
        return AuthenticatedDmResult(
            DmSendOutcome.DEFINITIVE_FAILURE, detail="malformed_request"
        )

    try:
        authorized = bool(is_authorized_existing_dm(request.platform, request.chat_id))
    except Exception:
        logger.warning(
            "authenticated DM authorization probe failed for %s; denying",
            request.platform,
            exc_info=True,
        )
        return AuthenticatedDmResult(
            DmSendOutcome.DEFINITIVE_FAILURE, detail="authorization_error"
        )
    if not authorized:
        # Not an existing authorized DM. This is definitive: we will not create
        # the chat and will not try another target.
        return AuthenticatedDmResult(
            DmSendOutcome.DEFINITIVE_FAILURE, detail="not_an_authorized_existing_dm"
        )

    try:
        adapter = resolve_transport(request.platform)
    except Exception:
        logger.warning(
            "authenticated DM transport resolution failed for %s",
            request.platform,
            exc_info=True,
        )
        return AuthenticatedDmResult(
            DmSendOutcome.DEFINITIVE_FAILURE, detail="transport_error"
        )
    if adapter is None:
        # No fallback: an unavailable transport is a definitive failure for
        # this attempt, and the caller's reservation stays intact for a later
        # deliberate retry.
        return AuthenticatedDmResult(
            DmSendOutcome.DEFINITIVE_FAILURE, detail="transport_unavailable"
        )

    send = getattr(adapter, "send", None)
    if not callable(send):
        return AuthenticatedDmResult(
            DmSendOutcome.DEFINITIVE_FAILURE, detail="transport_cannot_send"
        )

    try:
        outcome = send(
            request.chat_id,
            request.text,
            metadata={"idempotency_key": request.reservation_key},
        )
        if asyncio.iscoroutine(outcome):
            if run_coroutine is None:
                outcome.close()
                return AuthenticatedDmResult(
                    DmSendOutcome.DEFINITIVE_FAILURE, detail="no_event_loop_bridge"
                )
            outcome = run_coroutine(outcome)
    except Exception as exc:
        # The send raised *after* it may already have hit the wire. Anything
        # not definitively classifiable stays UNKNOWN so the caller preserves
        # the uncertain state instead of blind-retrying.
        classification = classify_send_error(f"{type(exc).__name__}: {exc}")
        logger.warning(
            "authenticated DM send raised for %s (%s)",
            request.platform,
            classification.value,
            exc_info=True,
        )
        return AuthenticatedDmResult(classification, detail="send_raised")

    error_text = _result_error_text(outcome)
    if error_text is not None:
        return AuthenticatedDmResult(
            classify_send_error(error_text), detail="send_failed"
        )
    if not _looks_successful(outcome):
        return AuthenticatedDmResult(DmSendOutcome.UNKNOWN, detail="indeterminate_result")

    return AuthenticatedDmResult(DmSendOutcome.SENT, receipt=_extract_receipt(outcome))


__all__ = [
    "classify_send_error",
    "send_authenticated_existing_dm",
]
