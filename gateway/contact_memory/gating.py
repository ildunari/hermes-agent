"""Deterministic Lane-A gate with per-session cooldown and topic state."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import re
import threading
from typing import Any, Mapping, Sequence

_DIRECT_REFERENCE = re.compile(r"^\s*(?:should|can|did|does|is|was|were|has|will)?\s*(?:i\s+)?(?:keep|sell|text|call)?\s*(it|he|she|they|that|this|the (?:one|car|thing|guy|girl|place))\b", re.I)
_ANTECEDENT = re.compile(r"\b(?:a|an|the|my|your|his|her|their)\s+[a-z][\w'-]+\b", re.I)


@dataclass(frozen=True)
class TurnState:
    register: str = "casual"
    closure: bool = False
    reaction: bool = False
    turn_index: int = 0
    now: float = 0.0
    current_vector: Any | None = None


@dataclass(frozen=True)
class GateDecision:
    retrieve: bool
    reasons: tuple[str, ...] = ()


@dataclass
class _SessionState:
    last_prefetch_at: float = float("-inf")
    last_prefetch_turn: int = -10_000
    centroid: Any | None = None
    last_message_at: float | None = None
    last_seen_at: float = 0.0


class LaneAGate:
    def __init__(self, *, cooldown_seconds: float = 90.0, cooldown_messages: int = 5, shift_similarity: float = 0.55, max_sessions: int = 1024, session_ttl_seconds: float = 6 * 60 * 60):
        self.cooldown_seconds = cooldown_seconds
        self.cooldown_messages = cooldown_messages
        self.shift_similarity = shift_similarity
        if max_sessions < 1 or session_ttl_seconds <= 0:
            raise ValueError("session bounds must be positive")
        self.max_sessions = int(max_sessions)
        self.session_ttl_seconds = float(session_ttl_seconds)
        self._sessions: OrderedDict[str, _SessionState] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def direct_reference(message: str, history: Sequence[Mapping[str, object]]) -> bool:
        if not _DIRECT_REFERENCE.search(message):
            return False
        recent = " ".join(str(row.get("content") or "") for row in history[-6:])
        return not bool(_ANTECEDENT.search(recent))

    def evaluate(self, session_key: str, message: str, history: Sequence[Mapping[str, object]], turn: TurnState) -> GateDecision:
        with self._lock:
            expired = [
                key for key, value in self._sessions.items()
                if turn.now - value.last_seen_at > self.session_ttl_seconds
            ]
            for key in expired:
                self._sessions.pop(key, None)
            state = self._sessions.get(session_key)
            if state is None:
                state = _SessionState()
                self._sessions[session_key] = state
            else:
                self._sessions.move_to_end(session_key)
            state.last_seen_at = turn.now
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
            gap = None if state.last_message_at is None else max(0.0, turn.now - state.last_message_at)
            state.last_message_at = turn.now
            reasons: list[str] = []
            vector = turn.current_vector
            if vector is not None and len(message.split()) >= 4:
                import numpy as np  # type: ignore[import-not-found]
                vector = np.asarray(vector, dtype=np.float32).reshape(-1)
                norm = float(np.linalg.norm(vector))
                if norm:
                    vector = vector / norm
                    if state.centroid is not None and state.centroid.size == vector.size:
                        similarity = float(state.centroid @ vector)
                        if gap is not None and gap > 45 * 60:
                            reasons.append("time_segment")
                        elif gap is not None and gap >= 5 * 60 and similarity < self.shift_similarity:
                            reasons.append("topic_shift")
                    state.centroid = vector if state.centroid is None or state.centroid.size != vector.size else state.centroid * 0.8 + vector * 0.2
                    state.centroid /= max(float(np.linalg.norm(state.centroid)), 1e-12)
            if turn.register in {"advice", "serious", "task"}:
                reasons.append("register")
            if self.direct_reference(message, history):
                reasons.append("direct_reference")
            if turn.closure or turn.reaction or not reasons:
                return GateDecision(False)
            if turn.now - state.last_prefetch_at < self.cooldown_seconds:
                return GateDecision(False)
            if turn.turn_index - state.last_prefetch_turn < self.cooldown_messages:
                return GateDecision(False)
            state.last_prefetch_at = turn.now
            state.last_prefetch_turn = turn.turn_index
            return GateDecision(True, tuple(dict.fromkeys(reasons)))
