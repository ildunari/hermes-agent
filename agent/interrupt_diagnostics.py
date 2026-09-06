"""Bounded local cancellation attribution without message text or frame locals."""
import hashlib
import logging
import re
import sys

logger = logging.getLogger("run_agent")


def _identity(value):
    # Session/request IDs can be supplied by integrations. Hash rather than
    # trusting them to be safe log text; reject oversized/non-string values.
    if type(value) is not str or not value or len(value) > 4096:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _generation(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _caller():
    """Identify the calling code only; never format a traceback or inspect arguments."""
    frame = sys._getframe(1)
    try:
        for _ in range(8):
            if frame is None:
                break
            module = frame.f_globals.get("__name__", "")
            if module not in {__name__, "agent.interrupt_control", "agent.interrupt_compat"}:
                source = f"{module}.{frame.f_code.co_name}"
                return source if len(source) <= 192 and re.fullmatch(r"[\w.<>]+", source) else "unknown"
            frame = frame.f_back
        return "unknown"
    finally:
        del frame


def record_cancellation(agent, *, reason, outcome, required_generation=None):
    """Record admission, not successful socket teardown. Call under the admission
    lock so identities/generation are sampled at the same edge as the decision.
    Reasons/outcomes are internal constants, never tool_reason or user input.
    No retained history, frame locals, or runtime state mutations.
    """
    event = {
        "reason": reason,
        "outcome": outcome,
        "session_id_sha256": _identity(getattr(agent, "session_id", None)),
        "api_request_id_sha256": _identity(getattr(agent, "_current_api_request_id", None)),
        "activity_generation": _generation(getattr(agent, "_turn_liveness_activity_generation", None)),
        "required_generation": _generation(required_generation),
        "source": _caller(),
    }
    # Also include the fixed-schema payload in the message: ordinary file
    # formatters do not serialize LogRecord.extra.
    try:
        logger.info("Cancellation admission: %s", event, extra={"cancellation": event})
    except Exception:
        # A broken logging handler must never prevent explicit Stop.
        pass
