"""Guest/family access policy helpers for gateway profile routing.

This module is intentionally pure and side-effect free: it does not start
platform adapters, switch profiles, or touch credentials.  It gives the
BlueBubbles gateway/router and the tool dispatcher a single policy vocabulary
that tests can exercise before the live router is enabled.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Iterator, Mapping, Sequence


_GUEST_POLICY_CONTEXT: ContextVar[bool] = ContextVar("hermes_guest_policy_context", default=False)


@contextmanager
def guest_policy_context(enabled: bool = True) -> Iterator[None]:
    """Enable guest tool policy for the current execution context only."""
    token = _GUEST_POLICY_CONTEXT.set(bool(enabled))
    try:
        yield
    finally:
        _GUEST_POLICY_CONTEXT.reset(token)


class GuestRoute(str, Enum):
    """High-level route decision for a BlueBubbles inbound event."""

    OWNER = "owner"
    GUEST = "guest"
    DENY = "deny"


@dataclass(frozen=True)
class ContactPolicy:
    """One approved contact and the surfaces/capabilities they may use."""

    contact_id: str
    display_name: str | None = None
    role: str = "family_guest"
    bluebubbles_handles: frozenset[str] = field(default_factory=frozenset)
    telegram_ids: frozenset[str] = field(default_factory=frozenset)
    discord_ids: frozenset[str] = field(default_factory=frozenset)
    email_addresses: frozenset[str] = field(default_factory=frozenset)
    allowed_surfaces: frozenset[str] = field(default_factory=lambda: frozenset({"bluebubbles"}))
    allowed_outbound_recipients: frozenset[str] = field(default_factory=lambda: frozenset({"self", "admin"}))
    tool_policy: str = "family_default"

    def bluebubbles_identity_set(self) -> frozenset[str]:
        return frozenset(
            normalize_identity(v)
            for v in self.bluebubbles_handles | self.email_addresses
            if normalize_identity(v)
        )


@dataclass(frozen=True)
class ContactRegistry:
    """Approved identities and owner aliases used by guest routing.

    Unknown identities are deliberately not represented here.  Adding a contact
    to this registry is the approval step Kosta controls.
    """

    owner_identities: frozenset[str] = field(default_factory=frozenset)
    contacts: tuple[ContactPolicy, ...] = ()
    admin_delivery_target: str | None = None
    guest_profile: str = "guest"
    owner_profile: str = "gpt"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ContactRegistry":
        data = data or {}
        owner_raw = data.get("owner_identities") or data.get("kosta_identities") or []
        contacts_raw = data.get("contacts") or {}
        contacts: list[ContactPolicy] = []
        if isinstance(contacts_raw, Mapping):
            iterable = contacts_raw.items()
        else:
            iterable = ((str(i), item) for i, item in enumerate(contacts_raw or []))
        for key, raw in iterable:
            if not isinstance(raw, Mapping):
                continue
            identities = raw.get("identities") or {}
            if not isinstance(identities, Mapping):
                identities = {}
            blue = identities.get("bluebubbles") or raw.get("bluebubbles") or {}
            if isinstance(blue, Mapping):
                bb_handles = blue.get("handles") or blue.get("ids") or []
            else:
                bb_handles = blue or []
            telegram = identities.get("telegram") or raw.get("telegram") or {}
            discord = identities.get("discord") or raw.get("discord") or {}
            email = identities.get("email") or raw.get("email") or {}
            contact_id = str(raw.get("id") or key)
            contacts.append(
                ContactPolicy(
                    contact_id=contact_id,
                    display_name=str(raw.get("display_name") or raw.get("name") or raw.get("label") or contact_id),
                    role=str(raw.get("role") or "family_guest"),
                    bluebubbles_handles=_norm_set(bb_handles),
                    telegram_ids=_norm_set(_ids_from_surface(telegram)),
                    discord_ids=_norm_set(_ids_from_surface(discord)),
                    email_addresses=_norm_set(_ids_from_surface(email, key="addresses")),
                    allowed_surfaces=frozenset(str(x).strip().lower() for x in raw.get("allowed_surfaces", ["bluebubbles"]) if str(x).strip()),
                    allowed_outbound_recipients=frozenset(str(x).strip().lower() for x in raw.get("allowed_outbound_recipients", ["self", "admin"]) if str(x).strip()),
                    tool_policy=str(raw.get("tool_policy") or "family_default"),
                )
            )
        return cls(
            owner_identities=_norm_set(owner_raw),
            contacts=tuple(contacts),
            admin_delivery_target=data.get("admin_delivery_target"),
            guest_profile=str(data.get("guest_profile") or "guest"),
            owner_profile=str(data.get("owner_profile") or "gpt"),
        )

    def find_bluebubbles_contact(self, identity: str | None) -> ContactPolicy | None:
        ident = normalize_identity(identity)
        if not ident:
            return None
        for contact in self.contacts:
            if ident in contact.bluebubbles_identity_set():
                return contact
        return None

    def is_owner_identity(self, identity: str | None) -> bool:
        ident = normalize_identity(identity)
        return bool(ident and ident in self.owner_identities)


@dataclass(frozen=True)
class BlueBubblesRouteDecision:
    route: GuestRoute
    profile: str | None
    contact_id: str | None = None
    contact_display_name: str | None = None
    contact_role: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class GuestToolDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False


_APPROVAL_REQUIRED_TOOLS = frozenset({
    "video_generate",
    "computer_use",
    "codex_subtask",
    "delegate_task",
    "send_message",
    "cronjob",
    "imessage_mini",
    "plik",
})

_ADMIN_ONLY_TOOLS = frozenset({
    "memory",
    "session_search",
    "telegram_actions",
    "fs",
})

_SENSITIVE_PATH_MARKERS = (
    "/.ssh",
    "/private_keys",
    "/.config/op",
    "/Library/Keychains",
    "/.hermes/config.yaml",
    "/.hermes/.env",
    "/.hermes/hermes-agent",
    "/.hermes/profiles/gpt",
    "/.hermes/profiles/default",
    "/.hermes/profiles/coding",
    "/.hermes/memories",
    "/.hermes/plugins",
    "/.hermes/cron",
    "/.codex",
    "/.claude",
    "/Library/Application Support/com.apple.TCC",
)

_PRIVATE_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "host.docker.internal"})


def _looks_private_or_local_url(value: Any) -> bool:
    try:
        parsed = urlparse(str(value))
    except Exception:
        return True
    if parsed.scheme not in {"http", "https"}:
        return True
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in _PRIVATE_HOSTNAMES or host.endswith(".local") or host.endswith(".lan") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _iter_web_urls(args: Mapping[str, Any]) -> Iterator[Any]:
    for key in ("url", "image_url"):
        if args.get(key):
            yield args.get(key)
    urls = args.get("urls")
    if isinstance(urls, Sequence) and not isinstance(urls, (str, bytes)):
        yield from urls
    elif urls:
        yield urls


def _web_action_can_fetch(args: Mapping[str, Any]) -> bool:
    action = str(args.get("action") or "").strip().lower()
    mode = str(args.get("mode") or "").strip().lower()
    return action in {"fetch", "answer", "summary", "json", "links", "curlmd"} or mode in {"markdown", "html", "answer", "summary", "json", "links"}


_SENSITIVE_COMMAND_PATTERNS = (
    r"\bop\s+",                  # 1Password CLI
    r"\bsecurity\s+find-",        # keychain reads
    r"\blaunchctl\s+",            # service control
    r"\bhermes\s+(gateway|update|profile|config|skills?)\b",
    r"\b(open|cat|less|more|tail|head)\s+[^\n]*(~|/Users/Kosta)(?![^\n]*\.hermes/profiles/guest)",
)


def _ids_from_surface(value: Any, key: str = "ids") -> Sequence[Any]:
    if isinstance(value, Mapping):
        return value.get(key) or value.get("handles") or value.get("addresses") or []
    return value or []


def _norm_set(values: Any) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, str):
        values = [values]
    return frozenset(v for v in (normalize_identity(x) for x in values) if v)


def normalize_identity(value: Any) -> str:
    """Normalize phone/email/chat identities for registry matching.

    This is intentionally conservative: display names never become owners by
    magic; only exact normalized handles listed in the registry match.
    """
    if value is None:
        return ""
    raw = str(value).strip().lower()
    if not raw:
        return ""
    if "imessage;-;" in raw or "sms;-;" in raw:
        raw = raw.split(";-;", 1)[1]
    if ";+;" in raw:
        return raw
    if "@" in raw and not re.fullmatch(r"[+()\d\s.-]+", raw):
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if raw.startswith("+") and digits:
        return "+" + digits
    return raw


def extract_bluebubbles_participant_identities(raw_message: Mapping[str, Any] | None) -> frozenset[str]:
    raw_message = raw_message or {}
    candidates: list[Any] = []

    def visit(obj: Any) -> None:
        if isinstance(obj, Mapping):
            for key in ("address", "handle", "phone", "email", "chatIdentifier", "identifier"):
                val = obj.get(key)
                if isinstance(val, Mapping):
                    visit(val)
                elif val:
                    candidates.append(val)
            for key in ("participants", "handles", "recipients"):
                val = obj.get(key)
                if isinstance(val, Sequence) and not isinstance(val, (str, bytes)):
                    for item in val:
                        visit(item)
        elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
            for item in obj:
                visit(item)

    visit(raw_message.get("participants"))
    visit(raw_message.get("handles"))
    data = raw_message.get("data") if isinstance(raw_message.get("data"), Mapping) else raw_message
    if isinstance(data, Mapping):
        visit(data.get("chats"))
        visit(data.get("participants"))
    return frozenset(v for v in (normalize_identity(x) for x in candidates) if v)


def approved_bluebubbles_contacts_in_message(
    raw_message: Mapping[str, Any] | None,
    registry: ContactRegistry,
) -> tuple[ContactPolicy, ...]:
    """Return approved BlueBubbles contacts present in a group payload."""
    participants = extract_bluebubbles_participant_identities(raw_message)
    if not participants:
        return ()
    found: list[ContactPolicy] = []
    seen: set[str] = set()
    for contact in registry.contacts:
        if contact.contact_id in seen or "bluebubbles" not in contact.allowed_surfaces:
            continue
        if participants & contact.bluebubbles_identity_set():
            found.append(contact)
            seen.add(contact.contact_id)
    return tuple(found)


def classify_bluebubbles_route(source: Any, raw_message: Mapping[str, Any] | None, registry: ContactRegistry) -> BlueBubblesRouteDecision:
    """Classify a BlueBubbles event as owner profile, guest profile, or deny.

    The sender decides the privilege level.  Kosta being present in an iMessage
    group is not enough to upgrade a guest sender into the owner profile.
    """
    sender = normalize_identity(getattr(source, "user_id", None) or getattr(source, "user_name", None))
    chat_type = (getattr(source, "chat_type", None) or "dm").lower()

    if registry.is_owner_identity(sender):
        return BlueBubblesRouteDecision(GuestRoute.OWNER, registry.owner_profile, reason="owner sender")

    contact = registry.find_bluebubbles_contact(sender)
    contact_allowed = bool(contact and "bluebubbles" in contact.allowed_surfaces)

    if chat_type == "group":
        if contact_allowed and contact is not None:
            return BlueBubblesRouteDecision(
                GuestRoute.GUEST,
                registry.guest_profile,
                contact.contact_id,
                contact.display_name,
                contact.role,
                "approved guest in group",
            )
        return BlueBubblesRouteDecision(GuestRoute.DENY, None, reason="unknown or unapproved sender in group")

    if contact_allowed and contact is not None:
        return BlueBubblesRouteDecision(
            GuestRoute.GUEST,
            registry.guest_profile,
            contact.contact_id,
            contact.display_name,
            contact.role,
            "approved guest dm",
        )

    return BlueBubblesRouteDecision(GuestRoute.DENY, None, reason="unknown or unapproved sender")


def load_contact_registry(path: str | os.PathLike[str] | None) -> ContactRegistry:
    """Load a YAML/JSON contact registry, returning an empty deny-all registry on missing path."""
    if not path:
        return ContactRegistry()
    p = Path(path).expanduser()
    if not p.exists():
        return ContactRegistry()
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        import yaml
        data = yaml.safe_load(text) or {}
    return ContactRegistry.from_dict(data)


def default_guest_sandbox_root() -> Path:
    return Path(os.environ.get("HERMES_GUEST_SANDBOX_ROOT") or "~/.hermes/profiles/guest/workspace").expanduser()


def resolve_under_sandbox(path_value: Any, sandbox_root: Path | None = None) -> Path | None:
    if not path_value:
        return None
    root = (sandbox_root or default_guest_sandbox_root()).expanduser().resolve()
    path = Path(str(path_value)).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except Exception:
        return None
    return resolved


def is_guest_policy_enabled() -> bool:
    return _GUEST_POLICY_CONTEXT.get() or str(os.environ.get("HERMES_GUEST_POLICY") or "").strip().lower() in {"1", "true", "yes", "on"}


def evaluate_guest_tool_call(function_name: str, function_args: Mapping[str, Any] | None, sandbox_root: Path | None = None) -> GuestToolDecision:
    """Return the guest policy decision for one tool call."""
    args = function_args or {}
    root = (sandbox_root or default_guest_sandbox_root()).expanduser().resolve()

    if function_name in _ADMIN_ONLY_TOOLS:
        return GuestToolDecision(False, f"{function_name} is admin-only for guest sessions")
    if function_name in _APPROVAL_REQUIRED_TOOLS:
        return GuestToolDecision(False, f"{function_name} requires Kosta/admin approval for guest sessions", requires_approval=True)

    if function_name == "fs":
        for key in ("path",):
            if args.get(key) and resolve_under_sandbox(args.get(key), root) is None:
                return GuestToolDecision(False, f"fs.{key} must stay inside guest sandbox {root}")
        return GuestToolDecision(True)

    if function_name in {"terminal", "process"}:
        if function_name == "process":
            # Keep process control in-scope of terminal sandboxing: only allow read/poll
            # style operations so guests can't perform arbitrary host process mutation.
            action = str(args.get("action") or "").strip().lower()
            if action in {"submit", "write", "close", "kill"}:
                return GuestToolDecision(False, f"process action '{action}' requires Kosta/admin approval for guest sessions", requires_approval=True)
            if action and action not in {"log", "poll", "wait"}:
                return GuestToolDecision(False, f"process action '{action}' is disabled for guest sessions")
            return GuestToolDecision(True)

        workdir = args.get("workdir") or str(root)
        if resolve_under_sandbox(workdir, root) is None:
            return GuestToolDecision(False, f"terminal workdir must stay inside guest sandbox {root}")
        command = str(args.get("command") or "")
        for marker in _SENSITIVE_PATH_MARKERS:
            if marker in command:
                return GuestToolDecision(False, f"terminal command references blocked path marker {marker}")
        for pattern in _SENSITIVE_COMMAND_PATTERNS:
            if re.search(pattern, command):
                return GuestToolDecision(False, "terminal command matches a blocked guest-session pattern")
        return GuestToolDecision(True)

    if function_name == "execute_code":
        code = str(args.get("code") or "")
        for marker in _SENSITIVE_PATH_MARKERS:
            if marker in code:
                return GuestToolDecision(False, f"execute_code references blocked path marker {marker}")
        if re.search(r"(/Users/Kosta|Path\(['\"]~|expanduser\(['\"]~)", code):
            return GuestToolDecision(False, "execute_code must not access host home paths in guest sessions")
        return GuestToolDecision(True)

    if function_name == "web":
        if _web_action_can_fetch(args):
            for url in _iter_web_urls(args):
                if _looks_private_or_local_url(url):
                    return GuestToolDecision(False, "web fetch/curl actions in guest sessions cannot access localhost, private-network, internal, or non-http URLs")
        return GuestToolDecision(True)

    if function_name == "web_search":
        return GuestToolDecision(True)

    if function_name == "skill":
        # Allow read-only skill access (list/view) so guests benefit from skill
        # knowledge, but block authoring/mutation. The unified `skill` tool routes
        # mutations through action='manage'; refuse that for guest sessions.
        action = str(args.get("action") or "").strip().lower()
        if action == "manage" or args.get("manage_action"):
            return GuestToolDecision(False, "skill authoring/management requires Kosta/admin approval for guest sessions", requires_approval=True)
        return GuestToolDecision(True)

    if function_name.startswith("browser_"):
        return GuestToolDecision(False, f"{function_name} is disabled for guest sessions", requires_approval=True)

    return GuestToolDecision(True)


def enforce_guest_tool_call(function_name: str, function_args: Mapping[str, Any] | None) -> str | None:
    """Return a JSON error string when guest policy blocks the tool call."""
    if not is_guest_policy_enabled():
        return None
    decision = evaluate_guest_tool_call(function_name, function_args)
    if decision.allowed:
        return None
    return json.dumps({
        "error": decision.reason,
        "requires_approval": decision.requires_approval,
        "guest_policy": True,
    }, ensure_ascii=False)
