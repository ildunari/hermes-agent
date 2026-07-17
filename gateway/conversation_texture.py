"""Deterministic per-turn texture guidance for human-like messaging profiles.

This module does not rewrite model output.  It adds a small ephemeral instruction
block before a turn so identity remains in SOUL.md while temporal style state
lives in code.  It is opt-in and profile-scoped through config.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


_SERIOUS_RE = re.compile(
    r"\b(died|death|dead|grief|funeral|cancer|mass|biopsy|hospital|scary test|"
    r"panic|spiral(?:ing)?|terrified|suicid|self[- ]?harm|unsafe|abuse|assault)\b",
    re.IGNORECASE,
)
_STYLE_COMPLAINT_RE = re.compile(
    r"\b(ai answer|sounds? like ai|robotic|too formal|whole speech|too much|"
    r"less formal|shorter|be brief|just pick|pick one)\b",
    re.IGNORECASE,
)
_HIGH_EFFORT_RE = re.compile(
    r"\b(explain|compare|research|analy[sz]e|debug|isolation order|"
    r"step by step|details?|source|verify|calculate|current (?:price|fact|rate))\b",
    re.IGNORECASE,
)
_TASK_RE = re.compile(
    r"\b(remind|schedule|set (?:it|a reminder)|draft|rewrite|look up|search|find|"
    r"send|post|call the|api|nginx|curl|timeout|error|502)\b",
    re.IGNORECASE,
)
_ADVICE_RE = re.compile(
    r"\b(should i|do i|would you|tell (?:her|him|them)|sell|keep it|leave it|"
    r"ignored|cheating|friend|coworker|double text|what should|pick one)\b",
    re.IGNORECASE,
)
_BURST_RE = re.compile(r"(?:\?\?|!!|\bwait\b|\bomg\b|\bwhat happened\b|\blmao\b)", re.IGNORECASE)
_CRAFT_RE = re.compile(
    r"\b(shrine|mugged by|audition for|miracle|salvation|ornament|universe voting|"
    r"better branding|tiny sun|criminal receipt|heavy lifting)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TextureConfig:
    enabled: bool = False
    low_weight: int = 68
    medium_weight: int = 25
    high_weight: int = 7
    burst_probability: float = 0.22
    exemplar_count: int = 3
    exemplar_path: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "TextureConfig":
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            low_weight=max(0, int(raw.get("low_weight", 68))),
            medium_weight=max(0, int(raw.get("medium_weight", 25))),
            high_weight=max(0, int(raw.get("high_weight", 7))),
            burst_probability=min(1.0, max(0.0, float(raw.get("burst_probability", 0.22)))),
            exemplar_count=max(0, min(8, int(raw.get("exemplar_count", 3)))),
            exemplar_path=str(raw["exemplar_path"]) if raw.get("exemplar_path") else None,
        )


def _unit_interval(*parts: object) -> float:
    material = "\x1f".join(str(p) for p in parts).encode("utf-8", "replace")
    value = int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "big")
    return value / float(2**64 - 1)


def sample_effort(session_key: str, turn_index: int, config: TextureConfig) -> str:
    total = config.low_weight + config.medium_weight + config.high_weight
    if total <= 0:
        return "low"
    pick = _unit_interval(session_key, turn_index, "effort") * total
    if pick < config.low_weight:
        return "low"
    if pick < config.low_weight + config.medium_weight:
        return "medium"
    return "high"


def load_exemplars(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    source = Path(path).expanduser()
    if not source.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict) and row.get("user") and row.get("assistant"):
                rows.append(row)
    except (OSError, ValueError, TypeError):
        return []
    return rows


def _content(row: Mapping[str, Any]) -> str:
    value = row.get("content", "")
    if isinstance(value, str):
        return value
    return str(value or "")


def _user_turn_count(history: Sequence[Mapping[str, Any]]) -> int:
    """Count the full thread; deterministic rolls must not freeze at the recent-window limit."""
    return sum(1 for row in history if row.get("role") == "user")


def _recent(history: Sequence[Mapping[str, Any]], role: str, limit: int = 4) -> list[str]:
    return [_content(row) for row in history if row.get("role") == role][-limit:]


def _register_for(message: str, recent_user_turns: Sequence[str], serious: bool) -> str:
    if serious:
        return "serious"
    if _TASK_RE.search(message) or _HIGH_EFFORT_RE.search(message):
        return "task"
    # A short answer can complete parameters requested by the immediately prior
    # task turn ("remind me..." -> "tomorrow" -> "morning"). Do not let task
    # register bleed into unrelated later banter.
    if any(_TASK_RE.search(turn) for turn in recent_user_turns[-2:]) and len(message.split()) <= 5:
        return "task"
    if _ADVICE_RE.search(message):
        return "advice"
    return "casual"


def _tags_for(message: str, register: str) -> set[str]:
    tags = {register}
    lower = message.lower()
    if "?" in message:
        tags.add("question")
    if register == "casual" and _BURST_RE.search(message):
        tags.add("reaction")
    if any(word in lower for word in ("car", "buy", "spent", "$", "sell")):
        tags.add("decision")
    if any(word in lower for word in ("text", "reply", "say it", "draft")):
        tags.add("draft")
    if register == "task" and _HIGH_EFFORT_RE.search(message):
        tags.add("technical")
    return tags


def _select_exemplars(
    exemplars: Iterable[Mapping[str, Any]],
    tags: set[str],
    session_key: str,
    turn_index: int,
    count: int,
) -> list[Mapping[str, Any]]:
    ranked: list[tuple[int, float, Mapping[str, Any]]] = []
    for i, row in enumerate(exemplars):
        row_tags = {str(x) for x in row.get("tags", [])}
        overlap = len(tags & row_tags)
        if overlap == 0:
            continue
        ranked.append((overlap, _unit_interval(session_key, turn_index, i, "example"), row))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in ranked[:count]]


def compile_turn_guidance(
    *,
    message: str,
    history: Sequence[Mapping[str, Any]],
    session_key: str,
    config: TextureConfig,
    exemplars: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Compile a compact, deterministic, API-only guidance block for one turn."""
    if not config.enabled:
        return ""

    user_turns = _recent(history, "user")
    assistant_turns = _recent(history, "assistant")
    turn_index = _user_turn_count(history)
    serious = bool(_SERIOUS_RE.search(message) or any(_SERIOUS_RE.search(x) for x in user_turns[-3:]))
    register = _register_for(message, user_turns, serious)
    style_complaint = bool(_STYLE_COMPLAINT_RE.search(message))
    joke_cooldown = bool(assistant_turns and _CRAFT_RE.search(assistant_turns[-1]))

    effort = sample_effort(session_key, turn_index, config)
    if register == "task":
        effort = "high" if _HIGH_EFFORT_RE.search(message) else "medium"
    elif register == "advice":
        effort = "medium"
    elif register == "casual" and effort == "high":
        effort = "medium"
    elif register == "serious" and effort == "high":
        effort = "medium"

    burst_allowed = (
        register == "casual"
        and (_BURST_RE.search(message) is not None or _unit_interval(session_key, turn_index, "burst") < config.burst_probability)
    )
    craft_allowed = (
        register == "casual"
        and not joke_cooldown
        and _unit_interval(session_key, turn_index, "craft") < 0.10
    )

    if effort == "low":
        effort_rule = (
            "For this non-task turn, reply with 1-5 words or one emoji unless that would be confusing. "
            "No metaphor, subordinate clause, explanation, or second polished sentence. A disposable reply like "
            "lol, oof, wait what, nice, fair, or yeah is a successful answer."
        )
    elif effort == "medium":
        effort_rule = "Give one natural thought, usually one short sentence. Useful beats quotable."
    else:
        effort_rule = "The user asked for real work or detail. Be complete, but keep chat rhythm and avoid ornamental cleverness."

    lines = [
        "<turn_texture private=\"true\">",
        f"target_effort: {effort}",
        f"register: {register}",
        f"serious_mode: {'on' if serious else 'off'}",
        f"joke_cooldown: {'on' if joke_cooldown else 'off'}",
        f"craft_allowed: {'yes' if craft_allowed else 'no'}",
        f"style_complaint: {'yes' if style_complaint else 'no'}",
        f"bubble_shape: {'burst_allowed' if burst_allowed else 'single_preferred'}",
        effort_rule,
    ]
    if serious:
        lines.append(
            "No joke. Be brief but emotionally present; do not turn support into a procedure. "
            "Do not poetically interpret the person's experience or explain their feelings back to them."
        )
    elif not craft_allowed:
        lines.append(
            "No crafted line this turn. Use literal everyday wording: no metaphor, aphorism, setup/punchline, "
            "balanced dualism, or sentence designed to be quoted."
        )
    if joke_cooldown:
        lines.append("The prior assistant reply was crafted. Make this reply plain and literal, with no punchline.")
    if style_complaint:
        lines.append("A style complaint is not new evidence. Improve phrasing without reversing advice unless facts changed.")
        if assistant_turns and re.search(r"shorter|less formal|too much|whole speech|be brief", message, re.IGNORECASE):
            lines.append(
                f"The prior reply was {len(assistant_turns[-1])} characters. The rewrite must be materially shorter "
                "and must not repeat the same wording. Preserve all requested facts."
            )
    if register == "task":
        lines.append(
            "This is a task turn. Texture may shorten the visible confirmation, but it must never suppress a required "
            "tool call or claim completion before tool success. Ask for missing required parameters instead of inventing them."
        )
    if burst_allowed:
        lines.append(
            "If a real person would react in two or three quick sends, you may output 2-3 tiny bubbles separated by one blank line. "
            "Do not split one polished paragraph artificially."
        )

    chosen = _select_exemplars(
        exemplars or [], _tags_for(message, register), session_key, turn_index, config.exemplar_count
    )
    if chosen:
        lines.append("Real-text texture examples. Copy the effort distribution, not wording or facts:")
        for row in chosen:
            lines.append(f"User: {str(row['user']).strip()}")
            lines.append(f"Friend: {str(row['assistant']).strip()}")
    lines.append("</turn_texture>")
    return "\n".join(lines)
