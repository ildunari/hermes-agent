"""Pure Discord rich UI card rendering helpers.

This module intentionally does not import discord.py, gateway clients, or
secrets. It builds serializable card models and REST-shaped Components V2
payload dictionaries that can be snapshot-tested without a live Discord bot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from typing import Any, Dict, Iterable, List, Optional


COMPONENTS_V2_FLAG = 1 << 15
MAX_COMPONENTS = 40
MAX_CUSTOM_ID_LENGTH = 100
MAX_BUTTON_LABEL_LENGTH = 80
MAX_SELECT_OPTIONS = 25
MAX_TEXT_DISPLAY_LENGTH = 3900

COPY_UNAUTHORIZED = "You don’t have permission to use this control."
COPY_ALREADY_RESOLVED = "This prompt was already resolved."
COPY_EXPIRED = "This prompt expired. Run the command again if you still want to continue."


_BUTTON_STYLES = {
    "primary": 1,
    "secondary": 2,
    "success": 3,
    "danger": 4,
    "link": 5,
}

_ACCENT_COLORS = {
    "neutral": 0x5865F2,
    "info": 0x5865F2,
    "success": 0x57F287,
    "warning": 0xFEE75C,
    "danger": 0xED4245,
}


def _truncate(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def _truncate_with_notice(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    notice = "\n\n[truncated for Discord UI; full value remains in the underlying request]"
    return value[: max(0, limit - len(notice) - 3)] + "..." + notice


def _stable_custom_id(kind: str, action: str, token: str = "") -> str:
    raw = ":".join(part for part in ("hrui", "v1", kind, action, token) if part)
    if len(raw) <= MAX_CUSTOM_ID_LENGTH:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    prefix = ":".join(part for part in ("hrui", "v1", kind, action) if part)
    trimmed_prefix = prefix[: MAX_CUSTOM_ID_LENGTH - 13]
    return f"{trimmed_prefix}:{digest}"


@dataclass(frozen=True)
class DiscordRichAction:
    """Serializable button or select action for a Discord rich card."""

    custom_id: str
    label: str
    style: str = "secondary"
    kind: str = "button"
    disabled: bool = False
    emoji: Optional[str] = None
    options: List[Dict[str, str]] = field(default_factory=list)
    url: Optional[str] = None

    def __post_init__(self) -> None:
        if len(self.custom_id) > MAX_CUSTOM_ID_LENGTH:
            raise ValueError("Discord button custom_id must be <= 100 characters")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscordRichAction":
        return cls(**dict(data))

    def to_component(self) -> Dict[str, Any]:
        if self.kind == "select":
            options = [
                {
                    "label": _truncate(opt.get("label", opt.get("value", "")), 100),
                    "value": _truncate(opt.get("value", opt.get("label", "")), 100),
                    **(
                        {"description": _truncate(opt["description"], 100)}
                        if opt.get("description")
                        else {}
                    ),
                }
                for opt in self.options[:MAX_SELECT_OPTIONS]
            ]
            return {
                "type": 3,
                "custom_id": self.custom_id,
                "placeholder": _truncate(self.label, 150),
                "disabled": self.disabled,
                "options": options,
            }

        component: Dict[str, Any] = {
            "type": 2,
            "style": _BUTTON_STYLES.get(self.style, _BUTTON_STYLES["secondary"]),
            "label": _truncate(self.label, MAX_BUTTON_LABEL_LENGTH),
            "disabled": self.disabled,
        }
        if self.style == "link":
            if not self.url:
                raise ValueError("Discord link buttons require a url")
            component["url"] = self.url
        else:
            component["custom_id"] = self.custom_id
        if self.emoji:
            component["emoji"] = {"name": self.emoji}
        return component


@dataclass(frozen=True)
class DiscordRichModalSpec:
    """Serializable intent for a future Discord modal handler."""

    custom_id: str
    title: str
    fields: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.custom_id) > MAX_CUSTOM_ID_LENGTH:
            raise ValueError("Discord modal custom_id must be <= 100 characters")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscordRichModalSpec":
        return cls(**dict(data))


@dataclass(frozen=True)
class DiscordRichCard:
    """Serializable card model for deterministic Discord UI surfaces."""

    kind: str
    title: str
    body: str = ""
    fields: List[Dict[str, str]] = field(default_factory=list)
    actions: List[DiscordRichAction] = field(default_factory=list)
    status: str = "neutral"
    footer: str = ""
    modal: Optional[DiscordRichModalSpec] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscordRichCard":
        payload = dict(data)
        payload["actions"] = [
            action if isinstance(action, DiscordRichAction) else DiscordRichAction.from_dict(action)
            for action in payload.get("actions", [])
        ]
        modal = payload.get("modal")
        if modal is not None and not isinstance(modal, DiscordRichModalSpec):
            payload["modal"] = DiscordRichModalSpec.from_dict(modal)
        return cls(**payload)


def _text_display(content: str) -> Dict[str, Any]:
    return {"type": 10, "content": _truncate(content, MAX_TEXT_DISPLAY_LENGTH)}


def _component_count(component: Dict[str, Any]) -> int:
    return 1 + sum(_component_count(child) for child in component.get("components", []))


def _append_with_budget(container: Dict[str, Any], component: Dict[str, Any], budget: List[int]) -> bool:
    needed = _component_count(component)
    if needed > budget[0]:
        return False
    container["components"].append(component)
    budget[0] -= needed
    return True


def _action_rows(actions: Iterable[DiscordRichAction]) -> Iterable[Dict[str, Any]]:
    buttons: List[Dict[str, Any]] = []
    for action in actions:
        component = action.to_component()
        if action.kind == "select":
            if buttons:
                yield {"type": 1, "components": buttons}
                buttons = []
            yield {"type": 1, "components": [component]}
            continue
        buttons.append(component)
        if len(buttons) == 5:
            yield {"type": 1, "components": buttons}
            buttons = []
    if buttons:
        yield {"type": 1, "components": buttons}


def build_components_v2_payload(card: DiscordRichCard) -> Dict[str, Any]:
    """Return a Discord REST-shaped Components V2 payload for *card*."""

    container: Dict[str, Any] = {
        "type": 17,
        "accent_color": _ACCENT_COLORS.get(card.status, _ACCENT_COLORS["neutral"]),
        "components": [],
    }
    budget = [MAX_COMPONENTS - 1]  # Top-level container consumes one component.

    _append_with_budget(container, _text_display(f"**{card.title}**"), budget)
    if card.body:
        _append_with_budget(container, _text_display(card.body), budget)

    for field_item in card.fields:
        name = field_item.get("name", "")
        value = field_item.get("value", "")
        text = f"**{name}**\n{value}" if name else value
        if not _append_with_budget(container, _text_display(text), budget):
            break

    if card.footer:
        _append_with_budget(container, _text_display(card.footer), budget)

    for row in _action_rows(card.actions):
        if not _append_with_budget(container, row, budget):
            break

    return {
        "flags": COMPONENTS_V2_FLAG,
        "components": [container],
    }


def build_legacy_embed_payload(card: DiscordRichCard) -> Dict[str, Any]:
    """Return a serializable legacy embed-style fallback payload."""

    return {
        "embed": {
            "title": card.title,
            "description": card.body,
            "color": _ACCENT_COLORS.get(card.status, _ACCENT_COLORS["neutral"]),
            "fields": list(card.fields),
            "footer": card.footer,
        },
        "actions": [action.to_dict() for action in card.actions],
    }


def approval_card(command: str, description: str = "dangerous command") -> DiscordRichCard:
    return DiscordRichCard(
        kind="approval",
        title="Approval required",
        body=f"```\n{_truncate_with_notice(command, 1800)}\n```",
        fields=[{"name": "Reason", "value": description or "dangerous command"}],
        status="warning",
        actions=[
            DiscordRichAction(_stable_custom_id("approval", "once"), "Allow once", "success"),
            DiscordRichAction(_stable_custom_id("approval", "session"), "Allow for session"),
            DiscordRichAction(_stable_custom_id("approval", "always"), "Always allow", "primary"),
            DiscordRichAction(_stable_custom_id("approval", "deny"), "Deny", "danger"),
        ],
    )


def confirmation_card(title: str, message: str, token: str = "") -> DiscordRichCard:
    return DiscordRichCard(
        kind="confirmation",
        title=title or "Confirmation required",
        body=_truncate_with_notice(message, 1800),
        status="warning",
        actions=[
            DiscordRichAction(_stable_custom_id("confirm", "once", token), "Allow once", "success"),
            DiscordRichAction(_stable_custom_id("confirm", "always", token), "Always allow", "primary"),
            DiscordRichAction(_stable_custom_id("confirm", "cancel", token), "Cancel", "danger"),
        ],
    )


def update_prompt_card(prompt: str, default: str = "") -> DiscordRichCard:
    default_hint = f"\n\nDefault: `{default}`" if default else ""
    return DiscordRichCard(
        kind="update_prompt",
        title="Update needs input",
        body=f"{_truncate_with_notice(prompt, 1800)}{default_hint}",
        status="warning",
        actions=[
            DiscordRichAction(_stable_custom_id("update", "yes"), "Yes", "success"),
            DiscordRichAction(_stable_custom_id("update", "no"), "No", "danger"),
        ],
    )


def clarify_prompt_card(question: str, choices: Optional[List[str]] = None, token: str = "") -> DiscordRichCard:
    clean_choices = [str(choice).strip() for choice in (choices or []) if str(choice).strip()]
    actions = [
        DiscordRichAction(
            _stable_custom_id("clarify", str(index), token),
            f"{index + 1}. {_truncate(choice, 72)}",
            "primary",
        )
        for index, choice in enumerate(clean_choices[:24])
    ]
    if clean_choices:
        actions.append(DiscordRichAction(_stable_custom_id("clarify", "other", token), "Other (type answer)"))
    return DiscordRichCard(
        kind="clarify",
        title="Hermes needs input",
        body=_truncate_with_notice(question, 1800),
        fields=[] if clean_choices else [{"name": "Reply", "value": "Reply in this channel with your answer."}],
        status="warning",
        actions=actions,
    )


def model_picker_card(
    providers: List[Dict[str, Any]],
    current_model: str,
    current_provider: str,
) -> DiscordRichCard:
    # Model catalogs can exceed Discord's 25-option select cap. Keep the
    # current provider visible when possible, then fill the remaining slots
    # in configured order and make the truncation explicit in the card copy.
    provider_pool = list(providers)
    current = [p for p in provider_pool if p.get("is_current")]
    rest = [p for p in provider_pool if not p.get("is_current")]
    visible_providers = (current + rest)[:MAX_SELECT_OPTIONS]
    hidden_count = max(0, len(provider_pool) - len(visible_providers))
    options = [
        {
            "label": provider.get("name") or provider.get("slug", "provider"),
            "value": provider.get("slug") or provider.get("name", ""),
            "description": "current" if provider.get("is_current") else "",
        }
        for provider in visible_providers
    ]
    hidden_note = (
        f"\nShowing {len(visible_providers)} of {len(provider_pool)} providers. "
        "Type `/model` with the provider/model name if the one you need is hidden."
        if hidden_count
        else ""
    )
    return DiscordRichCard(
        kind="model_picker",
        title="Model configuration",
        body=(
            f"Current model: `{current_model or 'unknown'}`\n"
            f"Provider: {current_provider or 'unknown'}"
            f"{hidden_note}"
        ),
        status="info",
        actions=[
            DiscordRichAction(
                _stable_custom_id("model", "provider"),
                "Choose a provider",
                kind="select",
                options=options,
            )
        ] if options else [],
    )


def run_status_card(run_id: str, status: str, detail: str = "") -> DiscordRichCard:
    return DiscordRichCard(
        kind="run_status",
        title="Run status",
        body=f"`{run_id}`\nStatus: **{status}**" + (f"\n\n{_truncate(detail, 1800)}" if detail else ""),
        status="info",
    )


def result_summary_card(title: str, summary: str, fields: Optional[List[Dict[str, str]]] = None) -> DiscordRichCard:
    return DiscordRichCard(
        kind="result_summary",
        title=title or "Result summary",
        body=_truncate(summary, 1800),
        fields=list(fields or []),
        status="success",
    )


def error_card(message: str, detail: str = "") -> DiscordRichCard:
    return DiscordRichCard(
        kind="error",
        title="Something went wrong",
        body=_truncate(message, 1800),
        fields=[{"name": "Detail", "value": _truncate(detail, 1800)}] if detail else [],
        status="danger",
    )


def expired_card() -> DiscordRichCard:
    return DiscordRichCard(
        kind="expired",
        title="Prompt expired",
        body=COPY_EXPIRED,
        status="warning",
    )


def unauthorized_card() -> DiscordRichCard:
    return DiscordRichCard(
        kind="unauthorized",
        title="Permission required",
        body=COPY_UNAUTHORIZED,
        status="danger",
    )


__all__ = [
    "COMPONENTS_V2_FLAG",
    "COPY_ALREADY_RESOLVED",
    "COPY_EXPIRED",
    "COPY_UNAUTHORIZED",
    "DiscordRichAction",
    "DiscordRichCard",
    "DiscordRichModalSpec",
    "approval_card",
    "build_components_v2_payload",
    "build_legacy_embed_payload",
    "clarify_prompt_card",
    "confirmation_card",
    "error_card",
    "expired_card",
    "model_picker_card",
    "result_summary_card",
    "run_status_card",
    "unauthorized_card",
    "update_prompt_card",
]
