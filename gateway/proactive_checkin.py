"""Deterministic, contact-local proactive check-ins and child-session generation.

Code owns timing.  The model is only asked to write the already-authorized words,
and those words are persisted in an isolated child session rather than appended
to the live conversation tail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import inspect
import re
import time
from typing import Any, Callable, Literal
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CheckinKind = Literal["serious", "open_loop"]
_DELAY_WINDOWS: dict[CheckinKind, tuple[float, float]] = {
    "serious": (3.0, 6.0),
    "open_loop": (1.5, 4.0),
}
_OPEN_LOOP_RE = re.compile(
    r"\b(?:wish me luck|interview|appointment|audition|exam|test|hearing|"
    r"meeting|date|flight|surgery|presentation)\b", re.I
)


def _unit(*parts: object) -> float:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8", "replace")
    value = int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "big")
    return value / float(2**64 - 1)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "UTC"))
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown contact timezone: {name}") from exc


def _clock_minutes(value: str) -> int:
    try:
        hour, minute = (int(part) for part in str(value).split(":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid active-hours time: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid active-hours time: {value!r}")
    return hour * 60 + minute


@dataclass(frozen=True)
class CheckinPlan:
    thread_key: str
    kind: CheckinKind
    send_at_ts: float
    reason: str
    timezone: str


@dataclass(frozen=True)
class CheckinInitiationResult:
    allowed: bool
    reason: str
    text: str = ""


def push_to_active_hours(
    timestamp: float,
    *,
    timezone: str,
    active_start: str = "09:00",
    active_end: str = "21:30",
    jitter_key: str = "",
    morning_jitter_minutes: int = 45,
) -> float:
    """Return *timestamp* when active, otherwise next contact-local morning.

    DST transitions are handled by ``zoneinfo`` and conversion through an aware
    datetime.  Morning jitter is deterministic and only moves forward from the
    configured opening, so the hard active-hours boundary is never violated.
    """
    tz = _timezone(timezone)
    local = datetime.fromtimestamp(float(timestamp), tz)
    start = _clock_minutes(active_start)
    end = _clock_minutes(active_end)
    if start >= end:
        raise ValueError("active hours must be a same-day increasing interval")
    minute = local.hour * 60 + local.minute
    if start <= minute < end:
        return float(timestamp)
    day = local.date() if minute < start else local.date() + timedelta(days=1)
    opening = datetime(day.year, day.month, day.day, start // 60, start % 60, tzinfo=tz)
    jitter = _unit(jitter_key, int(timestamp), timezone, "morning") * max(
        0, int(morning_jitter_minutes)
    )
    return opening.timestamp() + jitter * 60.0


def push_into_active_hours(
    timestamp: float,
    *,
    timezone_name: str,
    active_start: str = "09:00",
    active_end: str = "21:30",
    jitter_minutes: int = 45,
    jitter_key: str = "",
) -> float:
    """Compatibility spelling used by the scheduler and Phase-3 tests."""
    return push_to_active_hours(
        timestamp,
        timezone=timezone_name,
        active_start=active_start,
        active_end=active_end,
        jitter_key=jitter_key,
        morning_jitter_minutes=jitter_minutes,
    )


def plan_checkin(
    *,
    thread_key: str | None = None,
    contact_key: str | None = None,
    kind: CheckinKind,
    last_user_ts: float,
    already_sent_today: int = 0,
    reason: str,
    timezone: str | None = None,
    timezone_name: str | None = None,
    active_start: str = "09:00",
    active_end: str = "21:30",
) -> CheckinPlan | None:
    resolved_key = str(thread_key or contact_key or "").strip()
    resolved_timezone = str(timezone or timezone_name or "").strip()
    if not resolved_key:
        raise ValueError("thread_key/contact_key is required")
    if not resolved_timezone:
        raise ValueError("timezone is required")
    if already_sent_today >= 1:
        return None
    if kind not in _DELAY_WINDOWS:
        raise ValueError(f"unknown check-in kind: {kind}")
    lo, hi = _DELAY_WINDOWS[kind]
    delay = lo + _unit(resolved_key, int(last_user_ts // 86_400), kind, "delay") * (hi - lo)
    send_at = push_to_active_hours(
        float(last_user_ts) + delay * 3600.0,
        timezone=resolved_timezone,
        active_start=active_start,
        active_end=active_end,
        jitter_key=f"{resolved_key}:{kind}",
    )
    return CheckinPlan(resolved_key, kind, send_at, str(reason)[:500], resolved_timezone)


def should_cancel(
    plan: CheckinPlan,
    *,
    user_messaged_since: bool,
    thread_muted: bool,
    now_ts: float | None = None,
) -> bool:
    if user_messaged_since or thread_muted:
        return True
    now = time.time() if now_ts is None else float(now_ts)
    return now - plan.send_at_ts > 12 * 3600


def detect_checkin_kind(text: str, *, serious_tier: int = 0) -> CheckinKind | None:
    if int(serious_tier) > 0:
        return "serious"
    return "open_loop" if _OPEN_LOOP_RE.search(text or "") else None


_SERIOUS_BLOCK = """<checkin_texture private=\"true\">
Send ONE small unprompted check-in hours after a hard conversation ({reason}).
Use 3-9 plain words or one small question. No apology or reference to your
prior tone. No advice, resources, greeting ritual, corporate "checking in",
or emotionally needy language. One message only.
</checkin_texture>"""
_OPEN_LOOP_BLOCK = """<checkin_texture private=\"true\">
Send ONE tiny follow-up about: {reason}. Ask how it went in 2-8 words, matching
the thread's energy. No recap, advice, greeting ritual, or second message.
</checkin_texture>"""


def build_checkin_turn(plan: CheckinPlan) -> str:
    template = _SERIOUS_BLOCK if plan.kind == "serious" else _OPEN_LOOP_BLOCK
    return template.format(reason=plan.reason)


def assistant_first_parent_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return real parent history ending in its latest real user turn.

    No synthetic user row is manufactured.  The parent's already-sent assistant
    tail is intentionally excluded from the generation request, while remaining
    untouched and durable in the parent SessionDB row.
    """
    history = [
        dict(message) for message in messages
        if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
    ]
    first_user = next(
        (index for index, message in enumerate(history) if message.get("role") == "user"),
        None,
    )
    if first_user is None:
        raise ValueError("assistant-first generation requires a real parent user turn")
    history = history[first_user:]
    last_user = next(
        (index for index in range(len(history) - 1, -1, -1) if history[index].get("role") == "user"),
        None,
    )
    history = history[: int(last_user) + 1]
    repaired: list[dict[str, Any]] = []
    for message in history:
        if repaired and repaired[-1].get("role") == message.get("role"):
            # Keep the latest real row when a damaged/imported parent has two
            # adjacent rows of the same role; never manufacture a role.
            repaired[-1] = message
        else:
            repaired.append(message)
    roles = [message.get("role") for message in repaired]
    if not repaired or roles[-1] != "user" or any(
        role == roles[index - 1] for index, role in enumerate(roles) if index
    ):
        raise ValueError("parent history cannot be safely repaired to alternating roles")
    return repaired


@dataclass(frozen=True)
class ProactiveTurnRequest:
    """No-inbound model request with both lineage and alternating API history.

    Generators MUST send ``generation_history`` to the model. ``parent_history``
    is read-only audit/lineage context and may end in assistant; using it as the
    API tail would violate the assistant-first alternation contract.
    """

    parent_session_id: str
    cache_system_prompt: str
    execution_system_prompt: str
    parent_history: tuple[dict[str, Any], ...]
    generation_history: tuple[dict[str, Any], ...]
    kind: str


def run_proactive_child_turn(
    *,
    session_db: Any,
    parent_session_id: str,
    purpose_prompt: str,
    kind: str,
    generate: Callable[[ProactiveTurnRequest], Any],
    child_session_id: str | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Generate and atomically persist one isolated assistant-first child."""
    if session_db is None:
        raise RuntimeError("session database is required for proactive turns")
    parent = session_db.get_session(parent_session_id)
    if parent is None:
        raise KeyError(f"unknown parent session: {parent_session_id}")
    if str(parent.get("chat_type") or "dm") != "dm":
        raise ValueError("proactive turns are DM-only")
    stable_prompt = str(parent.get("system_prompt") or "")
    suffix = str(purpose_prompt or "").strip()
    execution_prompt = (stable_prompt + "\n\n" + suffix).strip() if suffix else stable_prompt
    history = tuple(
        dict(message) for message in session_db.get_messages(parent_session_id)
        if message.get("role") in {"user", "assistant", "tool"}
    )
    request = ProactiveTurnRequest(
        parent_session_id=parent_session_id,
        cache_system_prompt=stable_prompt,
        execution_system_prompt=execution_prompt,
        parent_history=history,
        generation_history=tuple(assistant_first_parent_history(list(history))),
        kind=kind,
    )
    generated = generate(request)
    if inspect.isawaitable(generated):
        raise TypeError("run_proactive_child_turn requires a synchronous generator")
    assistant_text = str(
        (generated.get("final_response") or "") if isinstance(generated, dict) else (generated or "")
    ).strip()
    if not assistant_text:
        raise ValueError("proactive generator returned no assistant content")
    child_id = child_session_id or f"proactive-{uuid.uuid4().hex}"
    session_db.create_initiated_assistant_child(
        parent_session_id=parent_session_id,
        child_session_id=child_id,
        assistant_content=assistant_text,
        initiated_kind=kind,
        timestamp=float(time.time() if now_ts is None else now_ts),
    )
    child = session_db.get_session(child_id)
    messages = session_db.get_messages(child_id)
    if child is None or child.get("parent_session_id") != parent_session_id:
        raise RuntimeError("proactive child lineage was not persisted")
    if [message.get("role") for message in messages] != ["assistant"]:
        raise RuntimeError("proactive child violated assistant-first alternation")
    if child.get("system_prompt") != stable_prompt:
        raise RuntimeError("proactive child cache prompt differs from parent")
    return {
        "session_id": child_id,
        "parent_session_id": parent_session_id,
        "final_response": assistant_text,
        "messages": messages,
    }
