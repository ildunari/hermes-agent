"""Deterministic conversation texture compiler v2.

V2 emits one internally consistent response/bubble plan. It is intentionally
separate from v1 so gateways can default to the established compiler and fail
open when v2 cannot compile.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_SERIOUS_T1_RE = re.compile(
    r"\b(passed away|funeral|grief|hospice|cancer|tumou?r|chemo|biopsy|miscarriage|"
    r"overdose|relapse|hospital|icu|suicid\w*|self[- ]?harm|unsafe|abuse|assault|"
    r"panic attack|scary test|emergency room)\b", re.I,
)
_DEATH_RE = re.compile(r"\b(died|dead|death|dying)\b", re.I)
_DEATH_SLANG_RE = re.compile(
    r"(i'?m (so |literally )?(dead|dying)|dead ?ass|died laughing|dying (of laughter|laughing|lol|lmao)|"
    r"dead (tired|serious)|\b(phone|battery|car|laptop|charger|engine|mic|controller|airpods?|watch)"
    r"('s| is| just)?\s*(died|dead|dying)|\bdead\b.{0,8}(lol|lmao|💀|😂)|💀)", re.I,
)
_SERIOUS_T2_RE = re.compile(
    r"\b(broke up|break ?up|divorce|dumped|cheat(?:ed|ing) on me|got fired|laid off|"
    r"lost my job|eviction|evicted|relapsed|spiral(?:ing)?|terrified|can'?t stop crying|falling apart)\b", re.I,
)
_LAUGH_RE = re.compile(r"\b(lol|lmao|lmfao|rofl|haha+|hehe+)\b|😂|🤣|😭", re.I)
_MASS_MEDICAL_RE = re.compile(r"(found|there'?s|they see|seeing) a mass|mass (on|in) (my|his|her|their|the)\b", re.I)
_TECHNICAL_RE = re.compile(
    r"\b(process|worker|thread|cron|job|service|daemon|server|prod|production|build|pipeline|"
    r"container|pod|cluster|cpu|memory|detector|sanitizer|compiler|runtime|command|script|api|http|nginx)\b", re.I,
)
_TECHNICAL_ACTION_RE = re.compile(r"\b(kill|killed|died|dead|death|unsafe|abuse|assault|spiral(?:ing)?)\b", re.I)
_STYLE_COMPLAINT_RE = re.compile(
    r"\b(ai answer|sounds? like (an? )?ai|robotic|too formal|whole speech|(?:that'?s|this is) (?:way )?too much|"
    r"less formal|shorter|be brief|just pick|pick one|stop hedging)\b", re.I,
)
_HIGH_EFFORT_RE = re.compile(
    r"\b(explain|compare|research|analy[sz]e|debug|isolation order|step by step|details?|sources?|"
    r"verify|calculate|walk me through|current (?:price|fact|rate|average)|"
    r"write (?:this|that|it).{0,24}\b\d{2,5} words?\b)\b", re.I,
)
_TASK_RE = re.compile(
    r"\b(remind me|remind us|schedule|set (?:it|a reminder|an alarm)|draft|rewrite|look up|search for|"
    r"send (?:it|that|the|him|her|them)|post (?:it|this)|book (?:a|the)|order (?:a|the)|"
    r"api|nginx|curl|timeout|stack ?trace|error log)\b", re.I,
)
_HTTP_5XX_RE = re.compile(r"\b(?:http|server|status|response|nginx|api|endpoint)\b.{0,35}\b5\d\d\b|\b5\d\d\b.{0,35}\b(?:error|status|response)\b", re.I)
_ADVICE_RE = re.compile(
    r"\b(should i|should we|do i|would you|tell (?:her|him|them)|is it (?:weird|bad|wrong)|"
    r"double text|what should|what do i (?:do|say)|worth it|keep it or|sell it or)\b", re.I,
)
_DIRECT_QUESTION_RE = re.compile(
    r"^\s*(?:who|what|when|where|why|how|which|whose|should|could|would|can|may|is|are|am|"
    r"do|does|did|will|was|were|have|has|had)\b|\b(?:right|yeah|no|okay|ok)\s*\?\s*$", re.I,
)
_BURST_RE = re.compile(r"(\?\?|!!|\bwait\b|\bomg\b|\bno way\b|\bwhat happened\b|\bguess what\b|\bfound out\b)", re.I)
_CLOSURE_RE = re.compile(
    r"^\s*(thanks?(?: you)?|thx|ty|tysm|ok(?:ay)?|k+|kk|bet|word|night|gn|good ?night|later|cya|"
    r"lol(?: thanks)?|lmao+|haha+|nice|cool|sweet|👍|❤️|🙏)[.!\s]*$", re.I,
)
_PIVOT_RE = re.compile(r"^\s*(?:anyway|anyways|on another note|random question|oh (?:btw|wait)|speaking of)|\bwhat(?:'s| is) for (?:dinner|lunch)\b", re.I)
_COMPLETION_RE = re.compile(r"\b(done|completed|scheduled|reminder (?:is )?set|sent successfully|booked|ordered|created)\b", re.I)
_PARAMETER_QUESTION_RE = re.compile(r"\b(when|what time|which day|what date|who|which one|where|recipient|for when)\b.*\?\s*$", re.I)
_CRAFT_STRUCT_RES = (
    re.compile(r"\bboth (?:are|can be) true\b", re.I),
    re.compile(r"\bcan be [^.,]{3,40}, and [^.,]{3,40} (?:too|as well)\b", re.I),
    re.compile(r"\bit'?s not [^.,]{3,30}, it'?s [^.,]{3,30}\b", re.I),
)
_VISIBLE_ROLES = frozenset({"user", "assistant"})
_TS_KEYS = ("ts", "timestamp", "date_epoch")


@dataclass(frozen=True)
class TextureConfig:
    enabled: bool = False
    response_classes: bool = True
    time_awareness: bool = True
    timezone: str = "UTC"
    low_weight: int = 68
    medium_weight: int = 25
    high_weight: int = 7
    burst_probability: float = 0.22
    follow_through_probability: float = 0.0
    exemplar_count: int = 3
    exemplar_path: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "TextureConfig":
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            response_classes=bool(raw.get("response_classes", True)),
            time_awareness=bool(raw.get("time_awareness", True)),
            timezone=str(raw.get("timezone") or "UTC"),
            low_weight=max(0, int(raw.get("low_weight", 68))),
            medium_weight=max(0, int(raw.get("medium_weight", 25))),
            high_weight=max(0, int(raw.get("high_weight", 7))),
            burst_probability=min(1.0, max(0.0, float(raw.get("burst_probability", .22)))),
            follow_through_probability=min(
                1.0, max(0.0, float(raw.get("follow_through_probability", 0.0)))
            ),
            exemplar_count=max(0, min(8, int(raw.get("exemplar_count", 3)))),
            exemplar_path=str(raw["exemplar_path"]) if raw.get("exemplar_path") else None,
        )


@dataclass(frozen=True)
class TurnFeatures:
    register: str
    serious_tier: int
    closure: bool
    question: bool
    burst_signal: bool
    laughter: bool
    style_complaint: bool
    high_effort_ask: bool
    gap_minutes: float | None
    local_hour: int | None
    timezone_name: str


@dataclass(frozen=True)
class ResponseSlot:
    response_class: str
    max_words: int


@dataclass(frozen=True)
class ResponsePlan:
    register: str
    slots: tuple[ResponseSlot, ...]
    craft_allowed: bool
    follow_through: bool = False

    @property
    def response_class(self) -> str:
        return self.slots[0].response_class


def _unit_interval(*parts: object) -> float:
    data = "\x1f".join(str(p) for p in parts).encode("utf-8", "replace")
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "big") / float(2**64 - 1)


def _sample_weighted(weights: Mapping[str, float], session_key: str, turn_index: int, salt: str) -> str:
    choices = [(name, weight) for name, weight in weights.items() if weight > 0]
    if not choices:
        return "ack"
    pick = _unit_interval(session_key, turn_index, salt) * sum(weight for _, weight in choices)
    total = 0.0
    for name, weight in choices:
        total += weight
        if pick < total:
            return name
    return choices[-1][0]


def sample_effort(session_key: str, turn_index: int, config: TextureConfig) -> str:
    return _sample_weighted(
        {"low": config.low_weight, "medium": config.medium_weight, "high": config.high_weight},
        session_key, turn_index, "effort",
    )


def _content(row: Mapping[str, Any]) -> str:
    value = row.get("content", "")
    return value if isinstance(value, str) else str(value or "")


def _row_ts(row: Mapping[str, Any]) -> float | None:
    for key in _TS_KEYS:
        value = row.get(key)
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            number = float(value)
            return number / 1000 if number > 10_000_000_000 else number
        if isinstance(value, str):
            try:
                number = float(value)
                return number / 1000 if number > 10_000_000_000 else number
            except ValueError:
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
    return None


def _user_turn_count(history: Sequence[Mapping[str, Any]]) -> int:
    return sum(row.get("role") == "user" for row in history)


def _recent(history: Sequence[Mapping[str, Any]], role: str, limit: int = 4) -> list[str]:
    return [_content(row) for row in history if row.get("role") == role][-limit:]


def _without_current(history: Sequence[Mapping[str, Any]], current_message_id: str | None) -> list[Mapping[str, Any]]:
    if current_message_id is None:
        return list(history)
    return [row for row in history if str(row.get("id") or row.get("message_id") or "") != str(current_message_id)]


def _is_question(message: str) -> bool:
    return "?" in message or bool(_DIRECT_QUESTION_RE.search(message.strip()))


def _tier_of(message: str) -> int:
    if _TECHNICAL_RE.search(message) and _TECHNICAL_ACTION_RE.search(message):
        return 0
    if _SERIOUS_T1_RE.search(message) or _MASS_MEDICAL_RE.search(message):
        return 1
    if _DEATH_RE.search(message) and not _DEATH_SLANG_RE.search(message):
        return 1
    if _SERIOUS_T2_RE.search(message):
        return 0 if _LAUGH_RE.search(message) else 2
    return 0


def _seriousness(message: str, recent_user_turns: Sequence[str]) -> int:
    current = _tier_of(message)
    if current:
        return current
    for prior in recent_user_turns[-3:]:
        tier = _tier_of(prior)
        if tier:
            return tier
    return 0


def _last_visible_ts(history: Sequence[Mapping[str, Any]]) -> float | None:
    for row in reversed(history):
        if row.get("role") in _VISIBLE_ROLES:
            stamp = _row_ts(row)
            if stamp is not None:
                return stamp
    return None


def _task_continuation(history: Sequence[Mapping[str, Any]]) -> bool:
    """Continue only an unresolved parameter/tool state at the history tail."""
    last_assistant_index = next(
        (index for index in range(len(history) - 1, -1, -1) if history[index].get("role") == "assistant"),
        None,
    )
    if last_assistant_index is None:
        return False
    last_assistant = history[last_assistant_index]
    if last_assistant.get("tool_calls"):
        # A later tool result resolves the call, whether it succeeded or failed.
        return not any(row.get("role") == "tool" for row in history[last_assistant_index + 1 :])
    if not _PARAMETER_QUESTION_RE.search(_content(last_assistant)):
        return False

    # Parameter chains may contain short user answers, but must trace back to an
    # explicit task without crossing completion or a normal assistant response.
    for row in reversed(history[:last_assistant_index]):
        role, text = row.get("role"), _content(row)
        if role == "tool" or role == "assistant" and _COMPLETION_RE.search(text):
            return False
        if role == "assistant" and not _PARAMETER_QUESTION_RE.search(text):
            return False
        if role == "user" and (_TASK_RE.search(text) or _HTTP_5XX_RE.search(text)):
            return True
    return False


def _safe_zone(name: str) -> tuple[ZoneInfo, str]:
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC"), "UTC"


def _extract_features(
    message: str,
    history: Sequence[Mapping[str, Any]],
    *,
    now_ts: float | None,
    time_awareness: bool,
    timezone_name: str,
) -> TurnFeatures:
    gap = None
    hour = None
    zone, zone_name = _safe_zone(timezone_name)
    if time_awareness and now_ts is not None:
        last_ts = _last_visible_ts(history)
        if last_ts is not None and now_ts > last_ts:
            gap = (now_ts - last_ts) / 60
        hour = datetime.fromtimestamp(now_ts, timezone.utc).astimezone(zone).hour

    current_tier = _tier_of(message)
    inherited_tier = 0
    strong_pivot = bool(_PIVOT_RE.search(message)) or gap is not None and gap >= 240
    if not current_tier and not strong_pivot:
        inherited_tier = _seriousness("", _recent(history, "user", 3))
    serious_tier = current_tier or inherited_tier

    explicit_task = bool(_TASK_RE.search(message) or _HTTP_5XX_RE.search(message) or _HIGH_EFFORT_RE.search(message))
    if serious_tier:
        register = "serious"
    elif explicit_task or _task_continuation(history):
        register = "task"
    elif _ADVICE_RE.search(message):
        register = "advice"
    else:
        register = "casual"
    question = _is_question(message)
    return TurnFeatures(
        register=register,
        serious_tier=serious_tier,
        closure=bool(_CLOSURE_RE.match(message.strip())) and register == "casual",
        question=question,
        burst_signal=bool(_BURST_RE.search(message)),
        laughter=bool(_LAUGH_RE.search(message)),
        style_complaint=bool(_STYLE_COMPLAINT_RE.search(message)),
        high_effort_ask=bool(_HIGH_EFFORT_RE.search(message)),
        gap_minutes=gap,
        local_hour=hour,
        timezone_name=zone_name,
    )


def _class_weights(features: TurnFeatures, craft_eligible: bool) -> Mapping[str, float]:
    if features.register == "serious":
        if features.question:
            return {"answer": 100}
        return {"reaction": 50, "ack": 30, "question": 15, "observation": 5}
    if features.register == "advice":
        if features.question:
            return {"answer": 100}
        return {"answer": 65, "question": 20, "next_step": 10, "observation": 5}
    if features.closure:
        return {"reaction": 80, "ack": 20}
    # Ordinary friends rarely reopen a declarative beat with an interview.
    # Keep unsolicited questions possible but exceptional; direct user
    # questions are handled by the explicit override below.
    weights = {"reaction": 44, "ack": 24, "answer": 17, "question": 2, "next_step": 3, "observation": 5, "craft": 5 if craft_eligible else 0}
    if features.question:
        weights.update({"answer": 60, "reaction": 10, "question": 0})
    if features.burst_signal:
        weights["reaction"] += 20
    return weights


def _select_class(features: TurnFeatures, session_key: str, turn_index: int, craft_eligible: bool) -> str:
    if features.register == "task":
        return "task"
    return _sample_weighted(_class_weights(features, craft_eligible), session_key, turn_index, "class")


def _looks_crafted(text: str) -> bool:
    return any(regex.search(text) for regex in _CRAFT_STRUCT_RES)


def _replay_prior_class(history: Sequence[Mapping[str, Any]], session_key: str, config: TextureConfig) -> str | None:
    """Replay every prior user turn with its actual eligibility state."""
    prefix: list[Mapping[str, Any]] = []
    prior_class = None
    for row in history:
        if row.get("role") != "user":
            prefix.append(row)
            continue
        message = _content(row)
        stamp = _row_ts(row)
        features = _extract_features(
            message, prefix, now_ts=stamp, time_awareness=config.time_awareness,
            timezone_name=config.timezone,
        )
        assistants = _recent(prefix, "assistant", 1)
        prior_output_crafted = bool(assistants and _looks_crafted(assistants[-1]))
        eligible = not (features.serious_tier or features.style_complaint or prior_class == "craft" or prior_output_crafted)
        prior_class = _select_class(features, session_key, _user_turn_count(prefix), eligible)
        prefix.append(row)
    return prior_class


def _make_plan(
    features: TurnFeatures,
    chosen: str,
    session_key: str,
    turn_index: int,
    burst_probability: float,
    follow_through_probability: float,
) -> ResponsePlan:
    caps = {"reaction": 5, "plain": 16, "ack": 10, "answer": 24, "question": 10, "next_step": 16, "observation": 24, "craft": 18, "task": 0}
    slots = [ResponseSlot(chosen, caps[chosen])]
    # Bursts need a semantic trigger in the incoming message. Randomly splitting
    # an ordinary declarative beat creates paragraph theatre and poor plan
    # compliance (for example, "thinking thai" becoming two forced bubbles).
    burst = (
        features.register == "casual"
        and chosen == "reaction"
        and not features.closure
        and features.burst_signal
        and _unit_interval(session_key, turn_index, "burst") < burst_probability
    )
    if burst:
        second = "question" if features.question is False and _unit_interval(session_key, turn_index, "burst-role") < .10 else "observation"
        slots = [ResponseSlot("reaction", 3), ResponseSlot(second, 8)]
    follow_through = (
        not burst
        and features.register == "casual"
        and chosen in {"answer", "observation"}
        and not features.closure
        and _unit_interval(session_key, turn_index, "follow-through") < follow_through_probability
    )
    if follow_through:
        slots.append(ResponseSlot("observation", 12))
    return ResponsePlan(
        features.register,
        tuple(slots),
        any(slot.response_class == "craft" for slot in slots),
        follow_through=follow_through,
    )


def load_exemplars(path: str | Path | None) -> list[dict[str, Any]]:
    if not path or not Path(path).expanduser().is_file():
        return []
    try:
        return [row for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines() if line.strip() and isinstance((row := json.loads(line)), dict) and row.get("user") and row.get("assistant")]
    except (OSError, ValueError, TypeError):
        return []


def _tags_for(message: str, features: TurnFeatures) -> set[str]:
    tags = {features.register}
    if features.question:
        tags.add("question")
    if features.closure:
        tags.add("closure")
    if any(word in message.lower() for word in ("car", "buy", "spent", "$", "sell")):
        tags.add("decision")
    return tags


def _select_exemplars(
    exemplars: Iterable[Mapping[str, Any]], tags: set[str], register: str,
    response_class: str, session_key: str, turn_index: int, count: int,
) -> list[Mapping[str, Any]]:
    ranked = []
    for index, row in enumerate(exemplars):
        row_tags = {str(tag) for tag in row.get("tags", [])}
        row_register = str(row.get("register") or (register if register in row_tags else ""))
        row_class = str(row.get("response_class") or "")
        if row_register != register or row_class != response_class:
            continue
        ranked.append((len(tags & row_tags), _unit_interval(session_key, turn_index, index, "example"), row))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in ranked[:count]]


def _effort_rule(effort: str) -> str:
    if effort == "low":
        return "For this non-task turn, reply with 1-5 words or one emoji unless confusing. A disposable reply is successful."
    if effort == "medium":
        return "Give one natural thought, usually one short sentence. Useful beats quotable."
    return "The user asked for real work or detail. Be complete, but keep chat rhythm."


def compile_turn_guidance(
    *,
    message: str,
    history: Sequence[Mapping[str, Any]],
    session_key: str,
    config: TextureConfig,
    exemplars: Sequence[Mapping[str, Any]] | None = None,
    now_ts: float | None = None,
    timezone_name: str | None = None,
    current_message_id: str | None = None,
    turn_ordinal: int | None = None,
    forced_register: str | None = None,
    forced_response_class: str | None = None,
    force_craft_ineligible: bool = False,
) -> str:
    if not config.enabled:
        return ""
    prior_history = _without_current(history, current_message_id)
    turn_index = _user_turn_count(prior_history)
    seed_ordinal = turn_index if turn_ordinal is None else int(turn_ordinal)
    zone_name = timezone_name or config.timezone
    features = _extract_features(
        message, prior_history, now_ts=now_ts, time_awareness=config.time_awareness,
        timezone_name=zone_name,
    )
    if forced_register is not None:
        if forced_register not in {"casual", "serious", "task", "advice"}:
            raise ValueError("invalid forced texture register")
        features = replace(features, register=forced_register, serious_tier=0)
    if forced_response_class is not None and forced_response_class not in {"reaction", "plain"}:
        raise ValueError("proactive forced response class must be reaction or plain")
    assistant_turns = _recent(prior_history, "assistant", 1)
    prior_class = _replay_prior_class(prior_history, session_key, config)
    cooldown = prior_class == "craft" or bool(assistant_turns and _looks_crafted(assistant_turns[-1]))
    craft_eligible = not (
        force_craft_ineligible or features.serious_tier or features.style_complaint or cooldown
    )
    chosen = forced_response_class or _select_class(
        features, session_key, seed_ordinal, craft_eligible
    )

    effort = sample_effort(session_key, seed_ordinal, config)
    if features.register == "task":
        effort = "high" if features.high_effort_ask else "medium"
    elif features.register == "advice":
        effort = "medium"
    elif effort == "high":
        effort = "medium"
    if chosen == "reaction":
        effort = "low"

    response_class = chosen if config.response_classes else "legacy"
    lines = [
        '<turn_texture private="true" engine="v2">',
        f"turn_index: {turn_index}",
        f"seed_turn_ordinal: {seed_ordinal}",
        f"register: {features.register}",
        f"response_class: {response_class}",
        f"target_effort: {effort}",
        f"serious_mode: {'on' if features.serious_tier else 'off'}",
        f"joke_cooldown: {'on' if cooldown else 'off'}",
        f"style_complaint: {'yes' if features.style_complaint else 'no'}",
    ]

    if not config.response_classes:
        lines.append("legacy_effort_rule: " + _effort_rule(effort))
        if chosen == "task":
            lines.append("Legacy task rule: perform required tool work, ask for missing required parameters, and never claim completion before tool success.")
    elif chosen == "task":
        lines += [
            "bubble_count: 1",
            "slot_1: class=task; cap=uncapped_for_required_work",
            "Task plan: perform every required tool call. Ask only for a missing required parameter, or confirm in one short sentence after tool success. Never claim completion before tool success or invent parameters.",
        ]
    else:
        plan = _make_plan(
            features,
            chosen,
            session_key,
            seed_ordinal,
            config.burst_probability,
            config.follow_through_probability,
        )
        lines.append(f"bubble_count: {len(plan.slots)}")
        for index, slot in enumerate(plan.slots, 1):
            lines.append(f"slot_{index}: class={slot.response_class}; max_words={slot.max_words}")
        lines.append(f"craft_allowed: {'yes' if plan.craft_allowed else 'no'}")
        lines.append(
            f"Output contract: render exactly {len(plan.slots)} bubble(s), no more and no fewer. "
            "Each slot is one sentence or fragment and must stay at or below its max_words cap. "
            "Use one blank line only between planned bubbles. Do not add an unplanned sentence, question, offer, or social action."
        )
        if not plan.craft_allowed:
            lines.append("Use literal everyday wording only; no metaphor, aphorism, setup/punchline, or quotable balanced line.")
        if plan.follow_through:
            lines.append(
                "Second bubble: keep the thread alive with one related detail or natural next thought. "
                "Make it a statement, not a question, offer, summary, or second punchline."
            )

    if features.serious_tier:
        lines.append("Serious thread: no jokes. Be present in plain words; do not narrate, poetically interpret, explain their feelings back, or turn support into an interview.")
        if features.question:
            lines.append("They asked a direct question. Answer it plainly even without question-mark punctuation.")
    if features.closure:
        lines.append("They are winding down. Match it and let it end; no question, offer, or new topic.")
    if features.style_complaint:
        lines.append("A style complaint is not new evidence. Improve phrasing without reversing advice unless facts changed.")
    if cooldown:
        lines.append("The prior reply spent the craft token. Stay plain and literal.")
    if features.gap_minutes is not None and features.gap_minutes >= 240:
        lines.append(f"time_gap: ~{round(features.gap_minutes / 60):.0f}h since the last visible conversational message; fresh beat, do not inherit old topic state.")
    elif features.gap_minutes is not None and features.gap_minutes >= 60:
        lines.append(f"time_gap: ~{round(features.gap_minutes / 60):.0f}h since the last visible conversational message; light re-entry.")
    if features.local_hour is not None:
        lines.append(f"local_time: {features.local_hour:02d}:xx; timezone: {features.timezone_name}")
        if features.local_hour >= 23 or features.local_hour < 6:
            lines.append("Late-night texture: lower energy and shorter; no unsolicited chores or plans.")

    exemplar_class = chosen
    selected = _select_exemplars(
        exemplars or [], _tags_for(message, features), features.register, exemplar_class,
        session_key, seed_ordinal, config.exemplar_count,
    )
    if selected:
        lines.append("Class-matched texture examples; copy shape only, never wording or facts:")
        for row in selected:
            lines.append(f"User: {str(row['user']).strip()}")
            lines.append(f"Friend: {str(row['assistant']).strip()}")
    lines.append("</turn_texture>")
    return "\n".join(lines)
