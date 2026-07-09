"""Deterministic recall cadence for external memory providers.

The policy is intentionally pure: it decides when to consume/queue recall and
builds bounded rolling queries, while MemoryManager remains responsible for
provider I/O. Dynamic recall stays API-call-time only, preserving prompt-cache
and role-alternation invariants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
from typing import Any, Deque, Dict, Iterable, List, Optional
from collections import deque

logger = logging.getLogger(__name__)


_DEFAULT_TRIGGER_TERMS = (
    "remember",
    "memory",
    "last time",
    "last session",
    "previous",
    "before",
    "again",
    "as before",
    "like before",
    "my setup",
    "my preference",
    "preference",
    "prefs",
    "convention",
    "local setup",
    "hermes",
    "gateway",
    "telegram",
    "discord",
    "imessage",
    "bluebubbles",
    "cron",
    "mac studio",
    "macbook",
    "mac mini",
    "1password",
    "credentials",
    "bookie",
    "stephen",
)

_TRIVIAL_RE = re.compile(
    r"^\s*(?:ok(?:ay)?|k|yes|yep|yeah|nah|no|thanks?|thx|cool|nice|got it|sure|do it|go ahead|continue|please do|sounds good)[.!?\s]*$",
    re.IGNORECASE,
)
_SLASH_RE = re.compile(r"^\s*/\w+(?:\s+.*)?$")
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryRecallConfig:
    enabled: bool = True
    cadence_turns: int = 4
    rolling_user_turns: int = 5
    max_query_chars: int = 1200
    max_prefetch_results: int = 5
    skip_trivial: bool = True
    log_events: bool = True
    trigger_terms: tuple[str, ...] = _DEFAULT_TRIGGER_TERMS
    topic_shift_jaccard_threshold: float = 0.18
    min_topic_words: int = 4

    @classmethod
    def from_mapping(cls, raw: Any) -> "MemoryRecallConfig":
        if not isinstance(raw, dict):
            raw = {}
        terms = raw.get("trigger_terms")
        if isinstance(terms, str):
            trigger_terms = tuple(t.strip().lower() for t in terms.split(",") if t.strip())
        elif isinstance(terms, (list, tuple)):
            trigger_terms = tuple(str(t).strip().lower() for t in terms if str(t).strip())
        else:
            trigger_terms = _DEFAULT_TRIGGER_TERMS
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            cadence_turns=max(1, _as_int(raw.get("cadence_turns"), 4)),
            rolling_user_turns=max(1, _as_int(raw.get("rolling_user_turns"), 5)),
            max_query_chars=max(160, _as_int(raw.get("max_query_chars"), 1200)),
            max_prefetch_results=max(1, _as_int(raw.get("max_prefetch_results"), 5)),
            skip_trivial=_as_bool(raw.get("skip_trivial"), True),
            log_events=_as_bool(raw.get("log_events"), True),
            trigger_terms=trigger_terms if trigger_terms is not None else _DEFAULT_TRIGGER_TERMS,
            topic_shift_jaccard_threshold=max(0.0, min(1.0, _as_float(raw.get("topic_shift_jaccard_threshold"), 0.18))),
            min_topic_words=max(1, _as_int(raw.get("min_topic_words"), 4)),
        )


@dataclass(frozen=True)
class RecallDecision:
    should_recall: bool
    reason: str
    query: str = ""
    immediate: bool = False
    trivial: bool = False


@dataclass
class RecallEvent:
    event: str
    turn_number: int
    reason: str = ""
    query_preview: str = ""
    result_chars: int = 0
    providers: int = 0


@dataclass
class MemoryRecallPolicy:
    config: MemoryRecallConfig = field(default_factory=MemoryRecallConfig)
    turn_number: int = 0
    substantive_turns: int = 0
    last_attempt_turn: int = 0
    last_success_turn: int = 0
    last_success_substantive_turn: int = 0
    last_injected_turn: int = 0
    empty_streak: int = 0
    error_streak: int = 0
    recent_user_messages: Deque[str] = field(default_factory=lambda: deque(maxlen=5))
    _last_topic_words: set[str] = field(default_factory=set)
    _pending_start_decision: Optional[RecallDecision] = None
    _pending_queue_decision: Optional[RecallDecision] = None
    events: List[RecallEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.recent_user_messages = deque(self.recent_user_messages, maxlen=self.config.rolling_user_turns)

    def on_turn_start(self, turn_number: int, message: str) -> RecallDecision:
        self.turn_number = max(int(turn_number or 0), self.turn_number + 1)
        text = _clean_message(message)
        trivial = is_trivial_message(text)
        trigger = self._trigger_reason(text)
        topic_shift = self._topic_shift_reason(text) if not trivial else ""

        if text and not trivial:
            self.substantive_turns += 1
            self.recent_user_messages.append(text)
            self._last_topic_words = _topic_words(text)
        cadence_due = self._cadence_due()

        if not self.config.enabled:
            decision = RecallDecision(False, "disabled", trivial=trivial)
        elif not text:
            decision = RecallDecision(False, "empty", trivial=True)
        elif self.config.skip_trivial and trivial:
            decision = RecallDecision(False, "trivial", trivial=True)
        else:
            reason = trigger or topic_shift or ("first_substantive_turn" if self.last_success_turn == 0 and not trivial else "")
            immediate = bool(trigger or topic_shift or (self.last_success_turn == 0 and not trivial))
            if not reason and cadence_due:
                reason = "cadence_due"
            if reason:
                decision = RecallDecision(True, reason, self.build_query(text), immediate=True, trivial=trivial)
            else:
                decision = RecallDecision(False, "cadence_not_due", trivial=trivial)

        self._pending_start_decision = decision
        self._record("turn_start", decision.reason, decision.query)
        return decision

    def on_turn_complete(self, message: str) -> RecallDecision:
        text = _clean_message(message)
        trivial = is_trivial_message(text)
        cadence_due = self._cadence_due()
        trigger = self._trigger_reason(text)
        topic_shift = self._topic_shift_reason(text) if not trivial else ""
        if not self.config.enabled:
            decision = RecallDecision(False, "disabled", trivial=trivial)
        elif not text:
            decision = RecallDecision(False, "empty", trivial=True)
        elif self.config.skip_trivial and trivial:
            decision = RecallDecision(False, "trivial", trivial=True)
        else:
            reason = trigger or topic_shift or ("cadence_due" if cadence_due else "")
            if reason:
                decision = RecallDecision(True, reason, self.build_query(text), immediate=False, trivial=trivial)
            else:
                decision = RecallDecision(False, "cadence_not_due", trivial=trivial)
        self._pending_queue_decision = decision
        return decision

    def note_queued(self, decision: RecallDecision, provider_count: int = 0) -> None:
        if decision.should_recall:
            self.last_attempt_turn = self.turn_number
            self._record("queued", decision.reason, decision.query, providers=provider_count)
        else:
            self._record("skipped_queue", decision.reason, decision.query, providers=provider_count)

    def note_prefetch_result(self, result: str, *, injected: bool, reason: str = "") -> None:
        result_chars = len(result or "")
        if result_chars and injected:
            self.last_success_turn = self.turn_number
            self.last_success_substantive_turn = self.substantive_turns
            self.last_injected_turn = self.turn_number
            self.empty_streak = 0
            self.error_streak = 0
            self._record("injected", reason or "non_empty", result_chars=result_chars)
        elif result_chars:
            # Drained a stale/skipped result without burning cadence.
            self._record("discarded", reason or "not_due", result_chars=result_chars)
        else:
            self.empty_streak += 1
            self._record("empty", reason or "no_prefetch_result")

    def note_error(self, reason: str) -> None:
        self.error_streak += 1
        self._record("error", reason)

    def reset(self) -> None:
        self.turn_number = 0
        self.substantive_turns = 0
        self.last_attempt_turn = 0
        self.last_success_turn = 0
        self.last_success_substantive_turn = 0
        self.last_injected_turn = 0
        self.empty_streak = 0
        self.error_streak = 0
        self.recent_user_messages.clear()
        self._last_topic_words.clear()
        self._pending_start_decision = None
        self._pending_queue_decision = None
        self.events.clear()

    def stats(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for event in self.events:
            counts[event.event] = counts.get(event.event, 0) + 1
        return {
            "enabled": self.config.enabled,
            "turn_number": self.turn_number,
            "substantive_turns": self.substantive_turns,
            "last_attempt_turn": self.last_attempt_turn,
            "last_success_turn": self.last_success_turn,
            "last_success_substantive_turn": self.last_success_substantive_turn,
            "last_injected_turn": self.last_injected_turn,
            "empty_streak": self.empty_streak,
            "error_streak": self.error_streak,
            "event_counts": counts,
        }

    def build_query(self, current_message: str = "") -> str:
        parts: List[str] = []
        seen = set()
        for msg in list(self.recent_user_messages):
            if msg and msg not in seen:
                parts.append(msg)
                seen.add(msg)
        current = _clean_message(current_message)
        if current and current not in seen and not is_trivial_message(current):
            parts.append(current)
        query = "\n".join(f"User turn: {p}" for p in parts[-self.config.rolling_user_turns:])
        if len(query) > self.config.max_query_chars:
            query = query[-self.config.max_query_chars:]
        return query.strip()

    def _cadence_due(self) -> bool:
        if self.substantive_turns <= 0:
            return False
        if self.last_success_substantive_turn <= 0:
            return True
        return (self.substantive_turns - self.last_success_substantive_turn) >= self.config.cadence_turns

    def _trigger_reason(self, text: str) -> str:
        if not text:
            return ""
        low = text.lower()
        for term in self.config.trigger_terms:
            if term and term in low:
                return f"trigger:{term}"
        return ""

    def _topic_shift_reason(self, text: str) -> str:
        words = _topic_words(text)
        if len(words) < self.config.min_topic_words or len(self._last_topic_words) < self.config.min_topic_words:
            return ""
        overlap = len(words & self._last_topic_words)
        union = len(words | self._last_topic_words)
        if union <= 0:
            return ""
        score = overlap / union
        if score <= self.config.topic_shift_jaccard_threshold:
            return "topic_shift"
        return ""

    def _record(self, event: str, reason: str = "", query: str = "", *, result_chars: int = 0, providers: int = 0) -> None:
        if not self.config.log_events:
            return
        preview = re.sub(r"\s+", " ", query or "").strip()[:160]
        self.events.append(RecallEvent(event, self.turn_number, reason, preview, result_chars, providers))
        if len(self.events) > 500:
            del self.events[:100]
        logger.debug(
            "memory recall %s turn=%s reason=%s providers=%s chars=%s query=%r",
            event,
            self.turn_number,
            reason,
            providers,
            result_chars,
            preview,
        )


def is_trivial_message(message: str) -> bool:
    text = _clean_message(message)
    if not text:
        return True
    if _SLASH_RE.match(text):
        return True
    if _TRIVIAL_RE.match(text):
        return True
    return False


def _clean_message(message: Any) -> str:
    if message is None:
        return ""
    text = str(message)
    text = re.sub(r"<memory-context>[\s\S]*?</memory-context>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _topic_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) >= 4}


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default
