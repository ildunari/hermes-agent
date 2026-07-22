"""Dark, browser-only transport state for the in-app browser.

This module deliberately contains no CDP backend.  It establishes the Phase-1
security waist used by later slices: one canonical wire contract, ephemeral
Desktop/chat association, compatibility negotiation, generations, and the
D-015 owner fence.  It is independent from the chat JSON-RPC dispatcher.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Iterable, Mapping, cast

_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CONTRACT_PATH = Path(__file__).with_name("browser_wire_v1.json")
WIRE_CONTRACT: dict[str, Any] = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def _method_hash_input(rows: Iterable[Mapping[str, Any]]) -> bytes:
    normalized = sorted(f"{row.get('direction', '')}:{row.get('name', '')}" for row in rows)
    return ("\n".join(normalized) + "\n").encode("utf-8")


def method_set_hash(rows: Iterable[Mapping[str, Any]] | None = None) -> str:
    """Return the deterministic SHA-256 id for names *and* directions."""

    selected = rows if rows is not None else WIRE_CONTRACT["required_methods"]
    return hashlib.sha256(_method_hash_input(selected)).hexdigest()


REQUIRED_METHODS = frozenset(row["name"] for row in WIRE_CONTRACT["required_methods"])
SUPPORTED_METHOD_SET_HASHES = frozenset({method_set_hash()})

# The WebSocket ceiling is necessarily server-wide under uvicorn. The smaller
# application cap is enforced again before a frame can enter a relay queue,
# leaving protocol/close-frame headroom at the WebSocket layer.
WEBSOCKET_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
APPLICATION_MAX_MESSAGE_BYTES = 50 * 1024 * 1024
RELAY_QUEUE_MAX_MESSAGES = 32
RELAY_QUEUE_MAX_BYTES = 64 * 1024 * 1024
RELAY_MAX_OUTSTANDING = 32
CONNECTION_MAX_OUTSTANDING = 64
TEARDOWN_RESERVE_MESSAGES = 1
TEARDOWN_RESERVE_BYTES = 64 * 1024
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_RELAY_ROLES = frozenset({"automation", "raw-cdp"})
_TEARDOWN_WRITE_TIMEOUT_SECONDS = 2.0
_RETIRED_BROWSER_SID_TTL_SECONDS = 300.0
_RETIRED_BROWSER_SID_LIMIT = 4096
_ORDINARY_OPERATION_TIMEOUT_SECONDS = 30.0
_LONG_OPERATION_TIMEOUT_SECONDS = 60.0
_MAX_OPERATION_TIMEOUT_SECONDS = 120.0
_LONG_OPERATION_METHODS = frozenset(
    {"Page.navigate", "Page.navigateToHistoryEntry", "Page.reload", "Runtime.awaitPromise"}
)
_browser_managers_lock = threading.Lock()
_browser_managers: weakref.WeakSet[BrowserTransportManager] = weakref.WeakSet()

BrowserFrameWriter = Callable[[bytes], Awaitable[bool]]


class BrowserProtocolError(ValueError):
    """Typed fail-dark browser protocol rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        generation: int | None = None,
        delivery: str = "not_started",
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.generation = generation
        self.delivery = delivery

    def outcome(self, *, generation: int | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "browser.outcome",
            "status": self.code,
            "retryable": self.retryable,
            "delivery": self.delivery,
        }
        selected_generation = self.generation if generation is None else generation
        if selected_generation is not None:
            result["capability_generation"] = selected_generation
        return result


@dataclass(frozen=True)
class BrowserTransportLimits:
    """Release queue limits, injectable at smaller values in behavior tests."""

    application_message_bytes: int = APPLICATION_MAX_MESSAGE_BYTES
    queue_messages: int = RELAY_QUEUE_MAX_MESSAGES
    queue_bytes: int = RELAY_QUEUE_MAX_BYTES
    relay_outstanding: int = RELAY_MAX_OUTSTANDING
    connection_outstanding: int = CONNECTION_MAX_OUTSTANDING
    teardown_messages: int = TEARDOWN_RESERVE_MESSAGES
    teardown_bytes: int = TEARDOWN_RESERVE_BYTES

    def __post_init__(self) -> None:
        values = (
            self.application_message_bytes,
            self.queue_messages,
            self.queue_bytes,
            self.relay_outstanding,
            self.connection_outstanding,
            self.teardown_messages,
            self.teardown_bytes,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("browser transport limits must be positive integers")
        if self.teardown_messages >= self.queue_messages:
            raise ValueError("teardown reserve must fit inside the queue message cap")
        if self.teardown_bytes >= self.queue_bytes:
            raise ValueError("teardown reserve must fit inside the queue byte cap")
        if self.application_message_bytes > self.queue_bytes - self.teardown_bytes:
            raise ValueError("application message cap must fit beside teardown reserve")
        if self.relay_outstanding > self.connection_outstanding:
            raise ValueError("per-relay outstanding limit cannot exceed connection limit")


DEFAULT_TRANSPORT_LIMITS = BrowserTransportLimits()


def validate_application_message_size(
    payload: str | bytes | bytearray | memoryview,
    *,
    limit: int = APPLICATION_MAX_MESSAGE_BYTES,
) -> int:
    """Enforce the decompressed, serialized application frame cap.

    This helper deliberately reports only a typed code; payload bytes and text
    never become diagnostics or exception messages.
    """

    if isinstance(payload, str):
        size = len(payload.encode("utf-8"))
    elif isinstance(payload, (bytes, bytearray, memoryview)):
        size = len(payload)
    else:
        raise BrowserProtocolError("browser_invalid_frame", "frame must be text or bytes")
    if size > limit:
        raise BrowserProtocolError(
            "browser_frame_too_large",
            "browser frame exceeds the application message cap",
        )
    return size


def _serialize_frame(payload: Any, *, limit: int) -> bytes:
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BrowserProtocolError(
            "browser_invalid_frame", "browser frame is not canonical JSON"
        ) from exc
    validate_application_message_size(serialized, limit=limit)
    return serialized


@dataclass(frozen=True, repr=False)
class BrowserQueuedFrame:
    """An immutable serialized queue entry; payload is never shown by repr."""

    serialized: bytes = field(repr=False)
    operation_id: str | None = None
    control: bool = False

    @property
    def size(self) -> int:
        return len(self.serialized)

    def decode(self) -> Any:
        return json.loads(self.serialized)

    def __repr__(self) -> str:
        return (
            "BrowserQueuedFrame("
            f"size={self.size}, control={self.control}, "
            f"operation={self.operation_id is not None})"
        )


class BoundedRelayQueue:
    """A count-and-byte-bounded FIFO with independent teardown reserve.

    Ordinary entries remain FIFO. The small control reserve is drained first so
    a saturated data queue cannot prevent cancel/close teardown. The reserve is
    carved out of (not added to) the total count and byte caps.
    """

    def __init__(self, limits: BrowserTransportLimits = DEFAULT_TRANSPORT_LIMITS) -> None:
        self._limits = limits
        self._lock = threading.RLock()
        self._normal: Deque[BrowserQueuedFrame] = deque()
        self._control: Deque[BrowserQueuedFrame] = deque()
        self._normal_bytes = 0
        self._control_bytes = 0

    @property
    def message_count(self) -> int:
        with self._lock:
            return len(self._normal)

    @property
    def byte_count(self) -> int:
        with self._lock:
            return self._normal_bytes

    @property
    def control_count(self) -> int:
        with self._lock:
            return len(self._control)

    @property
    def control_bytes(self) -> int:
        with self._lock:
            return self._control_bytes

    def put(
        self,
        payload: Any,
        *,
        operation_id: str | None = None,
        control: bool = False,
    ) -> BrowserQueuedFrame:
        serialized = _serialize_frame(
            payload,
            limit=(
                min(self._limits.application_message_bytes, self._limits.teardown_bytes)
                if control
                else self._limits.application_message_bytes
            ),
        )
        frame = BrowserQueuedFrame(serialized, operation_id=operation_id, control=control)
        with self._lock:
            if control:
                if (
                    len(self._control) >= self._limits.teardown_messages
                    or self._control_bytes + frame.size > self._limits.teardown_bytes
                ):
                    raise BrowserProtocolError(
                        "browser_overloaded", "browser teardown reserve is occupied", retryable=True
                    )
                self._control.append(frame)
                self._control_bytes += frame.size
                return frame
            if (
                len(self._normal)
                >= self._limits.queue_messages - self._limits.teardown_messages
                or self._normal_bytes + frame.size
                > self._limits.queue_bytes - self._limits.teardown_bytes
            ):
                raise BrowserProtocolError(
                    "browser_overloaded", "browser relay queue is full", retryable=True
                )
            self._normal.append(frame)
            self._normal_bytes += frame.size
            return frame

    def get(self) -> BrowserQueuedFrame | None:
        with self._lock:
            if self._control:
                frame = self._control.popleft()
                self._control_bytes -= frame.size
                return frame
            if not self._normal:
                return None
            frame = self._normal.popleft()
            self._normal_bytes -= frame.size
            return frame

    def remove_operation(self, operation_id: str) -> bool:
        with self._lock:
            for frame in self._normal:
                if frame.operation_id == operation_id:
                    self._normal.remove(frame)
                    self._normal_bytes -= frame.size
                    return True
        return False

    def clear(self) -> None:
        with self._lock:
            self._normal.clear()
            self._control.clear()
            self._normal_bytes = 0
            self._control_bytes = 0

    def diagnostics(self) -> dict[str, int]:
        with self._lock:
            return {
                "messages": len(self._normal),
                "bytes": self._normal_bytes,
                "control_messages": len(self._control),
                "control_bytes": self._control_bytes,
            }


def _decode_256_bit_value(value: str, field_name: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise BrowserProtocolError(
            f"browser_invalid_{field_name}",
            f"{field_name} must be canonical unpadded base64url",
        )
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except Exception as exc:
        raise BrowserProtocolError(
            f"browser_invalid_{field_name}", f"{field_name} must be base64url"
        ) from exc
    if len(decoded) != 32:
        raise BrowserProtocolError(
            f"browser_invalid_{field_name}", f"{field_name} must contain 256 bits"
        )
    return decoded


def validate_profile(profile: Any) -> str:
    if type(profile) is not str:
        raise BrowserProtocolError("browser_invalid_profile", "profile assertion must be a string")
    value = profile.strip()
    if not _PROFILE_RE.fullmatch(value):
        raise BrowserProtocolError("browser_invalid_profile", "profile assertion is invalid")
    return value


@dataclass(eq=False, repr=False)
class ChatAssociation:
    transport: object = field(repr=False)
    principal: str = field(repr=False)
    profile: str = field(repr=False)
    connection_id: str = field(repr=False)

    def __repr__(self) -> str:
        return "ChatAssociation(live=True)"


@dataclass(eq=False, repr=False)
class BrowserContext:
    transport: Any = field(repr=False)
    principal: str = field(repr=False)
    ticket_profile: str = field(repr=False)
    connection_id: str = field(repr=False)
    profile: str | None = field(default=None, repr=False)
    sid: str | None = field(default=None, repr=False)
    transport_id: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    present: bool = False
    state: str = "negotiating"
    capability_generation: int | None = None
    binding_generation: int | None = None
    method_set_hash: str | None = None
    chat_transport: object | None = field(default=None, repr=False)
    relays: list["RelayBinding"] = field(default_factory=list)
    tool_adapters: list["InAppBrowserToolAdapter"] = field(default_factory=list, repr=False)
    latest_task_generations: dict[str, int] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return (
            "BrowserContext("
            f"state={self.state!r}, present={self.present}, "
            f"capability_generation={self.capability_generation}, "
            f"binding_generation={self.binding_generation}, relays={len(self.relays)})"
        )


@dataclass(eq=False, repr=False)
class RelayBinding:
    context: BrowserContext = field(repr=False)
    relay_token: str = field(repr=False)
    capability_generation: int
    binding_generation: int
    sid: str = field(repr=False)
    profile: str = field(repr=False)
    chat_transport: object = field(repr=False)
    task_id: str = field(repr=False)
    tab_id: str = field(repr=False)
    guest_generation: str = field(repr=False)
    role: str = field(repr=False)
    task_generation: int
    _target_id: str = field(repr=False)
    outbound: BoundedRelayQueue = field(repr=False)
    inbound: BoundedRelayQueue = field(repr=False)
    active: bool = True
    state: str = "active"
    _operation_namespace: str = field(default_factory=lambda: secrets.token_urlsafe(12), repr=False)
    _next_operation: int = 0
    _operations: dict[str, "BrowserOperation"] = field(default_factory=dict, repr=False)
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _close_status: str | None = field(default=None, repr=False)
    _teardown_queued: bool = field(default=False, repr=False)

    @property
    def target_id(self) -> str:
        """The relay's immutable one-target binding."""

        return self._target_id

    @property
    def outstanding_count(self) -> int:
        return len(self._operations)

    def __repr__(self) -> str:
        return (
            "RelayBinding("
            f"state={self.state!r}, capability_generation={self.capability_generation}, "
            f"binding_generation={self.binding_generation}, target_bound=True, "
            f"outstanding={self.outstanding_count})"
        )


@dataclass(eq=False, repr=False)
class BrowserOperation:
    """One non-replayable command in a relay-local command-id namespace."""

    relay: RelayBinding = field(repr=False)
    operation_id: str
    request_id: int | str | None = field(default=None, repr=False)
    deadline_at: float = field(default=0.0, repr=False)
    timeout_seconds: float = field(default=_ORDINARY_OPERATION_TIMEOUT_SECONDS, repr=False)
    state: str = "created"
    terminal_status: str | None = None
    delivery: str | None = None
    retryable: bool = False
    _outcome: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def terminal(self) -> bool:
        return self.state == "terminal"

    @property
    def dispatched(self) -> bool:
        return self.state == "dispatched" or (
            self.terminal and self.delivery in {"outcome_unknown", "confirmed"}
        )

    @property
    def outcome(self) -> dict[str, Any] | None:
        return dict(self._outcome) if self._outcome is not None else None

    def __repr__(self) -> str:
        return (
            "BrowserOperation("
            f"state={self.state!r}, terminal_status={self.terminal_status!r}, "
            f"delivery={self.delivery!r})"
        )


@dataclass(eq=False, repr=False)
class InAppBrowserToolAdapter:
    """Ephemeral ownership of one relay plus the existing browser-tool route."""

    manager: "BrowserTransportManager" = field(repr=False)
    binding: RelayBinding = field(repr=False)
    relay: Any = field(repr=False)
    raw_binding: RelayBinding = field(repr=False)
    raw_relay: Any = field(repr=False)
    closed: bool = False

    @property
    def cdp_url(self) -> str:
        return str(self.relay.url)

    @property
    def raw_cdp_url(self) -> str:
        return str(self.raw_relay.url)

    def close(self, *, status: str = "browser_cancelled") -> bool:
        if self.closed:
            return False
        self.closed = True
        from tools.browser_tool import unregister_in_app_browser_session  # type: ignore[import-not-found]

        unregister_in_app_browser_session(
            task_id=self.binding.task_id,
            guest_generation=self.binding.guest_generation,
            task_generation=self.binding.task_generation,
        )
        self.manager.close_relay(self.binding, status=status)
        self.manager.close_relay(self.raw_binding, status=status)
        self.relay.stop()
        self.raw_relay.stop()
        with self.manager._lock:
            if self in self.binding.context.tool_adapters:
                self.binding.context.tool_adapters.remove(self)
        return True

    def __repr__(self) -> str:
        return f"InAppBrowserToolAdapter(closed={self.closed}, target_bound=True)"


class BrowserTransportManager:
    """Process-memory authority for chat/browser association and browser leases."""

    def __init__(
        self,
        *,
        limits: BrowserTransportLimits = DEFAULT_TRANSPORT_LIMITS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._limits = limits
        self._monotonic = monotonic
        self._chat: list[ChatAssociation] = []
        self._contexts: list[BrowserContext] = []
        self._capability_generation = 0
        self._binding_generation = 0
        # Collision detection is scoped to live/closing relays. Closed tuples are
        # rejected by their retired relay object, transport, sid and generations;
        # retaining random token tombstones for process lifetime would be unbounded.
        self._live_relay_tokens: set[str] = set()
        self._retired_sids: dict[str, float] = {}
        with _browser_managers_lock:
            _browser_managers.add(self)

    def _prune_retired_sids_locked(self) -> None:
        now = self._monotonic()
        for sid, expires_at in tuple(self._retired_sids.items()):
            if expires_at <= now:
                self._retired_sids.pop(sid, None)

    def _retire_sid_locked(self, sid: str | None) -> None:
        if not sid:
            return
        self._prune_retired_sids_locked()
        self._retired_sids.pop(sid, None)
        self._retired_sids[sid] = self._monotonic() + _RETIRED_BROWSER_SID_TTL_SECONDS
        while len(self._retired_sids) > _RETIRED_BROWSER_SID_LIMIT:
            self._retired_sids.pop(next(iter(self._retired_sids)))

    def owns_or_recently_retired_sid(self, sid: str) -> bool:
        """Whether *sid* belongs to this browser-only routing namespace."""

        if type(sid) is not str or not sid:
            return False
        with self._lock:
            self._prune_retired_sids_locked()
            return sid in self._retired_sids or any(
                context.sid == sid and context.state == "ready" for context in self._contexts
            )

    @property
    def association_count(self) -> int:
        with self._lock:
            return len(self._chat)

    @property
    def ready_count(self) -> int:
        with self._lock:
            return sum(1 for context in self._contexts if context.present)

    @property
    def live_relay_token_count(self) -> int:
        with self._lock:
            return len(self._live_relay_tokens)

    @property
    def contexts(self) -> tuple[BrowserContext, ...]:
        with self._lock:
            return tuple(self._contexts)

    @staticmethod
    def validate_connection_id(connection_id: Any) -> str:
        if type(connection_id) is not str:
            raise BrowserProtocolError(
                "browser_invalid_connection_id", "connection_id must be a string"
            )
        _decode_256_bit_value(connection_id, "connection_id")
        return connection_id

    def register_chat(
        self, *, transport: object, principal: str, profile: str, connection_id: str
    ) -> ChatAssociation:
        connection_id = self.validate_connection_id(connection_id)
        profile = validate_profile(profile)
        if not principal:
            raise BrowserProtocolError("browser_wrong_principal", "authenticated principal is absent")
        association = ChatAssociation(transport, principal, profile, connection_id)
        with self._lock:
            self._chat.append(association)
        return association

    def unregister_chat(self, transport: object) -> None:
        with self._lock:
            self._chat = [row for row in self._chat if row.transport is not transport]

    def new_browser_context(self, *, transport: Any, ticket: Mapping[str, Any]) -> BrowserContext:
        principal = str(ticket.get("principal") or "")
        if not principal:
            raise BrowserProtocolError("browser_wrong_principal", "ticket principal is absent")
        profile = validate_profile(ticket.get("profile"))
        connection_id = self.validate_connection_id(ticket.get("connection_id"))
        context = BrowserContext(
            transport=transport,
            principal=principal,
            ticket_profile=profile,
            connection_id=connection_id,
        )
        with self._lock:
            self._contexts.append(context)
        return context

    def _next_capability_generation(self) -> int:
        self._capability_generation += 1
        return self._capability_generation

    def _next_binding_generation(self) -> int:
        self._binding_generation += 1
        return self._binding_generation

    def _invalidate_relays(
        self, context: BrowserContext, *, status: str = "browser_stale_generation"
    ) -> None:
        for relay in tuple(context.relays):
            self._close_relay_locked(relay, status=status)
        context.relays.clear()
        context.present = False

    def _matching_chat(self, context: BrowserContext) -> ChatAssociation:
        rows = [row for row in self._chat if row.connection_id == context.connection_id]
        if not rows:
            raise BrowserProtocolError(
                "browser_unassociated", "no live chat socket has this Desktop association"
            )
        principal_rows = [row for row in rows if row.principal == context.principal]
        if not principal_rows:
            raise BrowserProtocolError(
                "browser_wrong_principal", "ticket principal does not own the chat association"
            )
        profile_rows = [row for row in principal_rows if row.profile == context.ticket_profile]
        if not profile_rows:
            raise BrowserProtocolError(
                "browser_wrong_profile", "ticket profile does not match the chat association"
            )
        return profile_rows[0]

    def validate_hello_claims(
        self, context: BrowserContext, hello: Mapping[str, Any]
    ) -> None:
        """Validate immutable ticket/hello claims before an association wait."""

        if not isinstance(hello, Mapping) or hello.get("type") != "client.hello":
            raise BrowserProtocolError("browser_invalid_hello", "first frame must be client.hello")
        profile = validate_profile(hello.get("profile"))
        connection_id = self.validate_connection_id(hello.get("connection_id"))
        if profile != context.ticket_profile:
            raise BrowserProtocolError(
                "browser_wrong_profile", "hello profile differs from its ticket assertion"
            )
        if connection_id != context.connection_id:
            raise BrowserProtocolError(
                "browser_wrong_connection", "hello connection_id differs from its ticket assertion"
            )

        browser = hello.get("browser")
        if not isinstance(browser, Mapping):
            raise BrowserProtocolError("browser_invalid_hello", "browser descriptor is required")
        if type(browser.get("present")) is not bool:
            raise BrowserProtocolError("browser_invalid_hello", "browser present must be a boolean")
        if type(browser.get("local_enabled")) is not bool:
            raise BrowserProtocolError(
                "browser_invalid_hello", "browser local_enabled must be a boolean"
            )
        protocol = browser.get("protocol")
        if not isinstance(protocol, Mapping):
            raise BrowserProtocolError("browser_invalid_hello", "browser protocol must be an object")
        if type(protocol.get("major")) is not int:
            raise BrowserProtocolError(
                "browser_invalid_hello", "browser protocol major must be an integer"
            )
        if type(protocol.get("minor")) is not int:
            raise BrowserProtocolError(
                "browser_invalid_hello", "browser protocol minor must be an integer"
            )
        if type(browser.get("method_set_hash")) is not str:
            raise BrowserProtocolError(
                "browser_invalid_hello", "browser method_set_hash must be a string"
            )
        methods = browser.get("methods")
        if type(methods) is not list or any(type(method) is not str for method in methods):
            raise BrowserProtocolError(
                "browser_invalid_hello", "browser methods must be a list of strings"
            )

    async def wait_for_chat(self, context: BrowserContext, *, timeout: float) -> None:
        """Wait briefly when the independently reconnecting browser arrives first.

        A live row for the same unguessable connection id with a different
        credential-derived principal or asserted profile is rejected immediately;
        only a genuinely absent chat leg is treated as a connection-order race.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            with self._lock:
                try:
                    self._matching_chat(context)
                    return
                except BrowserProtocolError as exc:
                    if exc.code != "browser_unassociated":
                        raise
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise BrowserProtocolError(
                    "browser_unassociated",
                    "no matching live chat socket joined before negotiation expired",
                    retryable=True,
                )
            await asyncio.sleep(min(0.05, remaining))

    def negotiate(
        self, context: BrowserContext, hello: Mapping[str, Any], *, server_enabled: bool
    ) -> dict[str, Any]:
        """Apply one compatibility hello and return a typed server outcome."""

        with self._lock:
            self._invalidate_relays(context)
            self._retire_sid_locked(context.sid)
            self.validate_hello_claims(context, hello)
            profile = validate_profile(hello.get("profile"))
            association = self._matching_chat(context)
            context.chat_transport = association.transport

            browser = hello.get("browser")
            if not isinstance(browser, Mapping):
                raise BrowserProtocolError("browser_invalid_hello", "browser descriptor is required")

            generation = self._next_capability_generation()
            binding_generation = self._next_binding_generation()
            context.capability_generation = generation
            context.binding_generation = binding_generation
            context.profile = profile
            context.sid = secrets.token_urlsafe(24)

            if (
                not server_enabled
                or browser.get("local_enabled") is not True
                or browser.get("present") is not True
            ):
                context.state = "disabled"
                context.present = False
                return {
                    "type": "server.hello",
                    "status": "browser_disabled",
                    "retryable": False,
                    "delivery": "not_started",
                    "capability_generation": generation,
                    "binding_generation": binding_generation,
                }

            protocol = browser.get("protocol")
            advertised_hash_value = browser.get("method_set_hash")
            advertised_hash = (
                advertised_hash_value if type(advertised_hash_value) is str else ""
            )
            advertised_methods = browser.get("methods")
            local_protocol = WIRE_CONTRACT["protocol"]
            remote_major = protocol.get("major") if isinstance(protocol, Mapping) else None
            remote_minor = protocol.get("minor") if isinstance(protocol, Mapping) else None
            methods_valid = (
                type(advertised_methods) is list
                and all(type(method) is str for method in advertised_methods)
            )
            normalized_methods = cast(list[str], advertised_methods) if methods_valid else []
            missing = sorted(REQUIRED_METHODS - set(normalized_methods))
            compatible = (
                isinstance(protocol, Mapping)
                and type(remote_major) is int
                and remote_major == local_protocol["major"]
                and type(remote_minor) is int
                and remote_minor == local_protocol["minor"]
                and type(advertised_hash_value) is str
                and advertised_hash in SUPPORTED_METHOD_SET_HASHES
                and methods_valid
                and not missing
            )
            if not compatible:
                context.state = "incompatible"
                context.present = False
                return {
                    "type": "server.hello",
                    "status": "browser_incompatible",
                    "retryable": False,
                    "delivery": "not_started",
                    "local": {
                        "protocol": dict(local_protocol),
                        "method_set_hash": method_set_hash(),
                    },
                    "remote": {
                        "protocol": dict(protocol) if isinstance(protocol, Mapping) else None,
                        "method_set_hash": advertised_hash,
                    },
                    "missing_methods": missing,
                    "capability_generation": generation,
                    "binding_generation": binding_generation,
                }

            context.method_set_hash = advertised_hash
            context.present = True
            context.state = "ready"
            return {
                "type": "server.hello",
                "status": "ready",
                "sid": context.sid,
                "transport_id": context.transport_id,
                "protocol": dict(local_protocol),
                "method_set_hash": method_set_hash(),
                "capability_generation": generation,
                "binding_generation": binding_generation,
            }

    def validate_upload_scope(
        self,
        *,
        principal: str,
        profile: str,
        connection_id: str,
        transport_id: str,
        browser_sid: str,
        capability_generation: str,
        binding_generation: str,
        task_id: str,
        task_generation: str,
        tab_id: str,
        source_session_id: str,
    ) -> None:
        """Authenticate an HTTP upload request against one live browser lease."""

        with self._lock:
            matches = [
                context
                for context in self._contexts
                if context.state == "ready"
                and context.present
                and context.principal == principal
                and context.profile == profile
                and context.connection_id == connection_id
                and context.transport_id == transport_id
                and context.sid == browser_sid
                and str(context.capability_generation) == capability_generation
                and str(context.binding_generation) == binding_generation
            ]
            if len(matches) != 1:
                raise BrowserProtocolError(
                    "browser_stale_generation", "upload browser lease is stale"
                )
            context = matches[0]
            if not any(
                relay.active and relay.task_id == task_id and relay.tab_id == tab_id
                and str(relay.task_generation) == task_generation
                and relay.task_id == source_session_id
                for relay in context.relays
            ):
                raise BrowserProtocolError(
                    "browser_task_not_bound", "upload task is not bound to this tab"
                )

    def bind_relay(
        self,
        context: BrowserContext,
        *,
        target_id: str | None = None,
        chat_transport: object | None = None,
        task_id: str = "legacy-task",
        tab_id: str = "legacy-tab",
        guest_generation: str = "legacy-guest",
        role: str = "automation",
        task_generation: int = 1,
    ) -> RelayBinding:
        with self._lock:
            if not context.present or context.sid is None or context.profile is None:
                raise BrowserProtocolError("browser_unavailable", "browser lease is not ready")
            association = self._matching_chat(context)
            selected_chat_transport = association.transport if chat_transport is None else chat_transport
            if selected_chat_transport is not association.transport:
                raise BrowserProtocolError(
                    "browser_wrong_chat_transport", "relay chat transport does not own this browser lease"
                )
            for value, field_name in (
                (task_id, "task_id"),
                (tab_id, "tab_id"),
                (guest_generation, "guest_generation"),
            ):
                if type(value) is not str or not value or len(value) > 256:
                    raise BrowserProtocolError(
                        f"browser_invalid_{field_name}", f"{field_name} is invalid"
                    )
            if role not in _RELAY_ROLES:
                raise BrowserProtocolError("browser_invalid_role", "relay role is invalid")
            if type(task_generation) is not int or task_generation <= 0:
                raise BrowserProtocolError(
                    "browser_invalid_task_generation", "task generation must be a positive integer"
                )
            if any(
                relay.active
                and relay.chat_transport is selected_chat_transport
                and relay.task_id == task_id
                and relay.role == role
                for relay in context.relays
            ):
                raise BrowserProtocolError(
                    "browser_task_already_bound", "task already owns a live browser relay"
                )
            if any(
                relay.active and relay.tab_id == tab_id and relay.role == role
                for relay in context.relays
            ):
                raise BrowserProtocolError(
                    "browser_tab_already_bound", "tab already has a live browser relay"
                )
            while True:
                relay_token = secrets.token_urlsafe(32)
                if relay_token not in self._live_relay_tokens:
                    break
            _decode_256_bit_value(relay_token, "relay_token")
            if target_id is None:
                target_id = secrets.token_urlsafe(18)
            if type(target_id) is not str or not target_id or len(target_id) > 256:
                raise BrowserProtocolError("browser_invalid_target", "one valid target is required")
            if context.capability_generation is None or context.binding_generation is None:
                raise BrowserProtocolError("browser_unavailable", "browser generations are absent")
            relay = RelayBinding(
                context=context,
                relay_token=relay_token,
                capability_generation=context.capability_generation,
                binding_generation=context.binding_generation,
                sid=context.sid,
                profile=context.profile,
                chat_transport=selected_chat_transport,
                task_id=task_id,
                tab_id=tab_id,
                guest_generation=guest_generation,
                role=role,
                task_generation=task_generation,
                _target_id=target_id,
                outbound=BoundedRelayQueue(self._limits),
                inbound=BoundedRelayQueue(self._limits),
            )
            self._live_relay_tokens.add(relay_token)
            context.relays.append(relay)
            return relay

    def resolve_relay(
        self,
        context: BrowserContext,
        *,
        chat_transport: object,
        task_id: Any,
        tab_id: Any,
        guest_generation: Any,
        role: Any,
        task_generation: Any,
        relay_token: Any,
    ) -> RelayBinding:
        """Resolve one operational relay by its complete immutable binding."""

        with self._lock:
            matches = [
                relay
                for relay in context.relays
                if relay.active
                and relay.chat_transport is chat_transport
                and type(task_id) is str
                and relay.task_id == task_id
                and type(tab_id) is str
                and relay.tab_id == tab_id
                and type(guest_generation) is str
                and relay.guest_generation == guest_generation
                and type(role) is str
                and relay.role == role
                and type(task_generation) is int
                and relay.task_generation == task_generation
                and type(relay_token) is str
                and hmac.compare_digest(relay.relay_token.encode(), relay_token.encode())
            ]
            if len(matches) != 1:
                raise BrowserProtocolError(
                    "browser_relay_not_found", "no exact live task/tab/role binding owns this frame"
                )
            return matches[0]

    def outbound_envelope(self, relay: RelayBinding, frame: BrowserQueuedFrame) -> dict[str, Any]:
        envelope = {
            "type": "browser.cdp.send",
            "sid": relay.sid,
            "profile": relay.profile,
            "capability_generation": relay.capability_generation,
            "binding_generation": relay.binding_generation,
            "relay_token": relay.relay_token,
            "task_id": relay.task_id,
            "tab_id": relay.tab_id,
            "guest_generation": relay.guest_generation,
            "role": relay.role,
            "task_generation": relay.task_generation,
            "operation_id": frame.operation_id,
            "frame": frame.decode(),
        }
        if frame.operation_id is not None:
            operation = relay._operations.get(frame.operation_id)
            if operation is not None:
                remaining = max(0.0, operation.deadline_at - self._monotonic())
                envelope["remaining_duration_ms"] = min(
                    int(remaining * 1000), int(_MAX_OPERATION_TIMEOUT_SECONDS * 1000)
                )
        return envelope

    def outbound_envelope_for_serialized(
        self, relay: RelayBinding, serialized: bytes
    ) -> dict[str, Any]:
        with self._lock:
            operation_id = next(
                (
                    operation.operation_id
                    for operation in relay._operations.values()
                    if operation.state == "writing"
                ),
                None,
            )
            return self.outbound_envelope(
                relay, BrowserQueuedFrame(serialized, operation_id=operation_id)
            )

    def accept_inbound_frame(
        self,
        context: BrowserContext,
        message: Mapping[str, Any],
        *,
        chat_transport: object,
    ) -> RelayBinding:
        """Fence one Desktop response/event, then make it visible to the relay."""

        relay = self.resolve_relay(
            context,
            chat_transport=chat_transport,
            task_id=message.get("task_id"),
            tab_id=message.get("tab_id"),
            guest_generation=message.get("guest_generation"),
            role=message.get("role"),
            task_generation=message.get("task_generation"),
            relay_token=message.get("relay_token"),
        )
        common = {
            "transport": context.transport,
            "sid": message.get("sid"),
            "profile": message.get("profile"),
            "capability_generation": message.get("capability_generation"),
            "relay_token": message.get("relay_token"),
            "binding_generation": message.get("binding_generation"),
        }
        payload = message.get("frame")
        operation_id = message.get("operation_id")
        if operation_id is None:
            self.authorize_frame(relay, **common)
            _serialize_frame(payload, limit=self._limits.application_message_bytes)
            if not isinstance(payload, Mapping) or type(payload.get("method")) is not str or "id" in payload:
                raise BrowserProtocolError(
                    "browser_invalid_event", "unsolicited browser frame must be a CDP event"
                )
        else:
            accepted = self.accept_operation_response(
                relay,
                operation_id=operation_id,
                payload=payload,
                failed=isinstance(payload, Mapping) and "error" in payload,
                **common,
            )
            if not accepted:
                raise BrowserProtocolError(
                    "browser_duplicate_frame", "late or duplicate browser response was rejected"
                )
        self.enqueue_frame(relay, payload, direction="inbound")
        return relay

    def dequeue_inbound(self, relay: RelayBinding) -> BrowserQueuedFrame | None:
        with self._lock:
            return relay.inbound.get()

    def chat_transport_for(self, context: BrowserContext) -> object:
        with self._lock:
            if context.chat_transport is None:
                raise BrowserProtocolError("browser_unassociated", "browser lease has no chat owner")
            return context.chat_transport

    def relay_duplex(self, relay: RelayBinding) -> "BrowserRelayDuplex":
        return BrowserRelayDuplex(self, relay)

    def bind_tool_adapter(
        self,
        context: BrowserContext,
        *,
        chat_transport: object,
        task_id: str,
        tab_id: str,
        guest_generation: str,
        task_generation: int,
    ) -> InAppBrowserToolAdapter:
        """Bind role-scoped relays directly into the unchanged twelve-tool adapter.

        The automation relay is consumed only by the trusted ``agent-browser``
        adapter.  The separate raw-CDP relay is selected by ``browser_cdp`` and
        reaches Electron under the narrower ``raw-cdp`` role.
        """
        from tools.browser_tool import (  # type: ignore[import-not-found]
            InAppBrowserSessionConflict,
            register_in_app_browser_session,
        )
        from tools.in_app_browser_relay import OnePageRelay  # type: ignore[import-not-found]

        for value, field_name in (
            (task_id, "task_id"),
            (tab_id, "tab_id"),
            (guest_generation, "guest_generation"),
        ):
            if type(value) is not str or not value or len(value) > 256:
                raise BrowserProtocolError(
                    f"browser_invalid_{field_name}", f"{field_name} is invalid"
                )
        if type(task_generation) is not int or task_generation <= 0:
            raise BrowserProtocolError(
                "browser_invalid_task_generation", "task generation must be a positive integer"
            )

        # A freshly authenticated successor socket can finish hello before the
        # predecessor route's ``finally`` block runs. The newer capability is
        # authoritative for this exact principal/profile/connection and retires
        # only the colliding task adapter; sibling tasks on both contexts stay
        # isolated. This also makes same-generation gateway reconnect a clean
        # re-declaration rather than an uncaught global-session collision.
        with self._lock:
            predecessor_adapters = tuple(
                adapter
                for candidate in self._contexts
                if candidate is not context
                and candidate.principal == context.principal
                and candidate.ticket_profile == context.ticket_profile
                and candidate.connection_id == context.connection_id
                and candidate.capability_generation is not None
                and context.capability_generation is not None
                and candidate.capability_generation < context.capability_generation
                for adapter in candidate.tool_adapters
                if not adapter.closed and adapter.binding.task_id == task_id
            )
        for predecessor in predecessor_adapters:
            predecessor.close(status="browser_stale_generation")

        with self._lock:
            existing = next(
                (
                    adapter
                    for adapter in context.tool_adapters
                    if not adapter.closed and adapter.binding.task_id == task_id
                ),
                None,
            )
            latest_generation = (
                context.latest_task_generations.get(task_id)
                if type(task_id) is str
                else None
            )
        if existing is not None:
            same = (
                existing.binding.tab_id == tab_id
                and existing.binding.guest_generation == guest_generation
                and existing.binding.task_generation == task_generation
            )
            if same:
                return existing
            if task_generation <= existing.binding.task_generation:
                raise BrowserProtocolError(
                    "browser_stale_task_generation", "task binding generation is stale"
                )
            existing.close(status="browser_stale_generation")
        elif (
            latest_generation is not None
            and type(task_generation) is int
            and task_generation <= latest_generation
        ):
            raise BrowserProtocolError(
                "browser_stale_task_generation", "task binding generation is stale"
            )

        binding = self.bind_relay(
            context,
            chat_transport=chat_transport,
            task_id=task_id,
            tab_id=tab_id,
            guest_generation=guest_generation,
            role="automation",
            task_generation=task_generation,
        )
        try:
            raw_binding = self.bind_relay(
                context,
                chat_transport=chat_transport,
                task_id=task_id,
                tab_id=tab_id,
                guest_generation=guest_generation,
                role="raw-cdp",
                task_generation=task_generation,
            )
        except Exception:
            self.close_relay(binding, status="browser_unavailable")
            raise

        from tools.browser_tool import invalidate_in_app_browser_session_refs  # type: ignore[import-not-found]

        relay = OnePageRelay(
            self.relay_duplex(binding),
            on_top_level_commit=lambda: invalidate_in_app_browser_session_refs(
                task_id=task_id,
                guest_generation=guest_generation,
                task_generation=task_generation,
            ),
        )
        raw_relay = OnePageRelay(self.relay_duplex(raw_binding))

        try:
            relay.start()
            raw_relay.start()
            try:
                register_in_app_browser_session(
                    task_id=task_id,
                    cdp_url=relay.url,
                    raw_cdp_url=raw_relay.url,
                    profile=binding.profile,
                    connection_id=context.connection_id,
                    capability_generation=binding.capability_generation,
                    tab_id=tab_id,
                    binding_generation=binding.binding_generation,
                    guest_generation=guest_generation,
                    task_generation=task_generation,
                    snapshot_identity_provider=relay.take_accessibility_snapshots,
                )
            except InAppBrowserSessionConflict as exc:
                # A successor socket may finish hello before its predecessor's
                # finally block retires the old adapter. Keep that collision
                # task-scoped instead of destroying healthy sibling relays.
                raise BrowserProtocolError(
                    "browser_task_already_bound",
                    "task already owns a live browser adapter",
                    retryable=True,
                ) from exc
        except Exception:
            self.close_relay(binding, status="browser_unavailable")
            self.close_relay(raw_binding, status="browser_unavailable")
            if relay.url:
                relay.stop()
            if raw_relay.url:
                raw_relay.stop()
            raise

        adapter = InAppBrowserToolAdapter(self, binding, relay, raw_binding, raw_relay)
        with self._lock:
            context.tool_adapters.append(adapter)
            context.latest_task_generations[task_id] = task_generation
        return adapter

    def unbind_tool_adapter(
        self,
        context: BrowserContext,
        *,
        task_id: Any,
        tab_id: Any,
        guest_generation: Any,
        task_generation: Any,
        status: str = "browser_cancelled",
    ) -> bool:
        """Close only the exact live task generation; stale teardown is inert."""

        with self._lock:
            adapter = next(
                (
                    candidate
                    for candidate in context.tool_adapters
                    if not candidate.closed
                    and type(task_id) is str
                    and candidate.binding.task_id == task_id
                    and type(tab_id) is str
                    and candidate.binding.tab_id == tab_id
                    and type(guest_generation) is str
                    and candidate.binding.guest_generation == guest_generation
                    and type(task_generation) is int
                    and candidate.binding.task_generation == task_generation
                ),
                None,
            )
        return adapter.close(status=status) if adapter is not None else False

    def close_context_adapters(
        self, context: BrowserContext, *, status: str = "browser_disconnected"
    ) -> int:
        with self._lock:
            adapters = tuple(context.tool_adapters)
        return sum(adapter.close(status=status) for adapter in adapters)

    def bump_binding_generation(self, context: BrowserContext) -> int:
        with self._lock:
            for relay in tuple(context.relays):
                self._close_relay_locked(relay, status="browser_stale_generation")
            context.relays.clear()
            context.binding_generation = self._next_binding_generation()
            return context.binding_generation

    @staticmethod
    def _queue_for(relay: RelayBinding, direction: str) -> BoundedRelayQueue:
        if direction == "outbound":
            return relay.outbound
        if direction == "inbound":
            return relay.inbound
        raise BrowserProtocolError("browser_invalid_direction", "queue direction is invalid")

    def enqueue_frame(
        self,
        relay: RelayBinding,
        payload: Any,
        *,
        direction: str,
    ) -> BrowserQueuedFrame:
        """Enqueue a non-operation frame in either independently bounded direction."""

        with self._lock:
            if not relay.active or relay.state != "active":
                raise BrowserProtocolError("browser_unavailable", "browser relay is not active")
            return self._queue_for(relay, direction).put(payload)

    def _connection_outstanding_locked(self, context: BrowserContext) -> int:
        return sum(relay.outstanding_count for relay in context.relays)

    def _next_operation_id_locked(self, relay: RelayBinding) -> str:
        relay._next_operation += 1
        return f"{relay._operation_namespace}:{relay._next_operation}"

    def admit_operation(
        self,
        relay: RelayBinding,
        payload: Any,
    ) -> BrowserOperation:
        """Atomically admit and enqueue one command, or reject before dispatch."""

        with self._lock:
            if not relay.active or relay.state != "active" or relay not in relay.context.relays:
                raise BrowserProtocolError("browser_unavailable", "browser relay is not active")
            # The manager is the sole id authority. A monotonically increasing
            # relay-local sequence never addresses a successor and needs no
            # process-lifetime or bounded tombstone cache.
            operation_id = self._next_operation_id_locked(relay)
            if relay.outstanding_count >= self._limits.relay_outstanding:
                raise BrowserProtocolError(
                    "browser_overloaded",
                    "relay outstanding limit reached",
                    retryable=True,
                    generation=relay.capability_generation,
                )
            if (
                self._connection_outstanding_locked(relay.context)
                >= self._limits.connection_outstanding
            ):
                raise BrowserProtocolError(
                    "browser_overloaded",
                    "connection outstanding limit reached",
                    retryable=True,
                    generation=relay.capability_generation,
                )

            request_id = payload.get("id") if isinstance(payload, Mapping) else None
            method = payload.get("method") if isinstance(payload, Mapping) else None
            params = payload.get("params") if isinstance(payload, Mapping) else None
            waits_for_promise = (
                method == "Runtime.evaluate"
                and isinstance(params, Mapping)
                and params.get("awaitPromise") is True
            )
            timeout_seconds = (
                _LONG_OPERATION_TIMEOUT_SECONDS
                if method in _LONG_OPERATION_METHODS or waits_for_promise
                else _ORDINARY_OPERATION_TIMEOUT_SECONDS
            )
            timeout_seconds = min(timeout_seconds, _MAX_OPERATION_TIMEOUT_SECONDS)
            operation = BrowserOperation(
                relay=relay,
                operation_id=operation_id,
                request_id=request_id,
                deadline_at=self._monotonic() + timeout_seconds,
                timeout_seconds=timeout_seconds,
            )
            # Queue first: count, byte, or application-cap rejection must leave
            # no admitted id behind and is unambiguously not_started.
            try:
                relay.outbound.put(payload, operation_id=operation_id)
            except BrowserProtocolError as exc:
                exc.generation = relay.capability_generation
                raise
            operation.state = "admitted"
            relay._operations[operation_id] = operation
            return operation

    def _settle_operation_locked(
        self,
        operation: BrowserOperation,
        *,
        status: str,
        retryable: bool,
        delivery: str | None = None,
    ) -> bool:
        if operation.terminal:
            return False
        relay = operation.relay
        was_dispatched = operation.state == "dispatched"
        if delivery is None:
            delivery = "outcome_unknown" if was_dispatched else "not_started"
        if delivery == "outcome_unknown":
            # This bit is consumed by generic retry machinery. A side-effecting
            # frame that may have reached the page is never safe to auto-retry.
            retryable = False
        relay.outbound.remove_operation(operation.operation_id)
        relay._operations.pop(operation.operation_id, None)
        operation.state = "terminal"
        operation.terminal_status = status
        operation.delivery = delivery
        operation.retryable = retryable
        operation._outcome = {
            "type": "browser.outcome",
            "operation_id": operation.operation_id,
            "status": status,
            "retryable": retryable,
            "delivery": delivery,
            "capability_generation": relay.capability_generation,
        }
        return True

    @staticmethod
    def _status_retryable(status: str) -> bool:
        return status in {
            "browser_disconnected",
            "browser_crashed",
            "browser_stale_generation",
            "browser_unavailable",
            "browser_timed_out",
            "browser_write_rejected",
        }

    def _finalize_close_locked(self, relay: RelayBinding, *, status: str) -> bool:
        if relay.state == "closed":
            return False
        for operation in tuple(relay._operations.values()):
            # A writer which has not returned acceptance when its transport is
            # torn down is uncertain, not a proven pre-accept rejection.
            delivery = "outcome_unknown" if operation.state == "writing" else None
            self._settle_operation_locked(
                operation,
                status=status,
                retryable=self._status_retryable(status),
                delivery=delivery,
            )
        relay.outbound.clear()
        relay.inbound.clear()
        relay.active = False
        relay.state = "closed"
        relay._teardown_queued = False
        self._live_relay_tokens.discard(relay.relay_token)
        if relay in relay.context.relays:
            relay.context.relays.remove(relay)
        return True

    def _begin_close_locked(
        self,
        relay: RelayBinding,
        *,
        status: str,
        queue_teardown: bool,
    ) -> bool:
        if relay.state == "closed":
            return False
        changed = relay.state != "closing"
        if changed:
            relay.state = "closing"
            relay.active = False
            relay._close_status = status
            # A write already in progress is settled only after its acceptance
            # result is known. Everything else can be classified immediately.
            for operation in tuple(relay._operations.values()):
                if operation.state != "writing":
                    self._settle_operation_locked(
                        operation,
                        status=status,
                        retryable=self._status_retryable(status),
                    )
        if queue_teardown and not relay._teardown_queued:
            try:
                relay.outbound.put({"type": "browser.relay.close"}, control=True)
                relay._teardown_queued = True
            except BrowserProtocolError:
                # The reserve is bounded. If it is unexpectedly unavailable,
                # closure still wins and no additional work can be admitted.
                relay._teardown_queued = False
        return changed

    async def write_next(
        self,
        relay: RelayBinding,
        writer: BrowserFrameWriter,
    ) -> BrowserQueuedFrame | None:
        """Dequeue, write, and classify one outbound frame as one operation.

        ``writer`` is the narrow transport seam: it must return exactly ``True``
        only after the transport accepts the serialized frame for async send, and
        ``False`` for a proven pre-accept rejection. Exceptions mean acceptance is
        uncertain. No caller can manually manufacture a dispatched transition.
        Teardown control frames use this same write-coupled path.
        """

        async with relay._write_lock:
            with self._lock:
                if relay.state == "closed":
                    return None
                frame = relay.outbound.get()
                if frame is None:
                    return None
                operation = (
                    relay._operations.get(frame.operation_id)
                    if frame.operation_id is not None
                    else None
                )
                if frame.operation_id is not None and (
                    operation is None or operation.state != "admitted"
                ):
                    raise BrowserProtocolError(
                        "browser_operation_state", "queued operation is not admissible"
                    )
                if operation is not None:
                    operation.state = "writing"

            try:
                accepted = await writer(frame.serialized)
            except asyncio.CancelledError:
                with self._lock:
                    if operation is not None and not operation.terminal:
                        operation.state = "dispatched"
                    self._begin_close_locked(
                        relay, status="browser_disconnected", queue_teardown=False
                    )
                    self._finalize_close_locked(relay, status="browser_disconnected")
                raise
            except Exception as exc:
                with self._lock:
                    if operation is not None and not operation.terminal:
                        operation.state = "dispatched"
                    self._begin_close_locked(
                        relay, status="browser_disconnected", queue_teardown=False
                    )
                    self._finalize_close_locked(relay, status="browser_disconnected")
                raise BrowserProtocolError(
                    "browser_write_uncertain",
                    "browser transport write acceptance is uncertain",
                    delivery="outcome_unknown",
                ) from exc

            with self._lock:
                if type(accepted) is not bool:
                    accepted = False
                if (
                    operation is not None
                    and operation.terminal
                    and operation.delivery == "confirmed"
                ):
                    # A correctly fenced matching response may race ahead of
                    # the writer coroutine's return. That response is stronger
                    # proof of dispatch than a contradictory late False result;
                    # never overwrite its frozen exactly-once outcome.
                    accepted = True
                if not accepted:
                    status = relay._close_status or "browser_write_rejected"
                    if operation is not None and not operation.terminal:
                        self._settle_operation_locked(
                            operation,
                            status=status,
                            retryable=self._status_retryable(status),
                            delivery="not_started",
                        )
                    self._begin_close_locked(relay, status=status, queue_teardown=False)
                    self._finalize_close_locked(relay, status=status)
                    raise BrowserProtocolError(
                        "browser_write_rejected",
                        "browser transport rejected the frame before acceptance",
                        retryable=True,
                        generation=relay.capability_generation,
                    )

                if operation is not None and not operation.terminal:
                    operation.state = "dispatched"
                    if relay.state == "closing":
                        status = relay._close_status or "browser_cancelled"
                        self._settle_operation_locked(
                            operation,
                            status=status,
                            retryable=self._status_retryable(status),
                            delivery="outcome_unknown",
                        )
                if frame.control:
                    relay._teardown_queued = False
                    if relay.state == "closing":
                        self._finalize_close_locked(
                            relay, status=relay._close_status or "browser_cancelled"
                        )
                return frame

    async def _close_with_writer(
        self,
        relay: RelayBinding,
        *,
        status: str,
        writer: BrowserFrameWriter,
    ) -> bool:
        with self._lock:
            changed = self._begin_close_locked(relay, status=status, queue_teardown=True)
            needs_teardown = relay.state == "closing" and relay._teardown_queued
        try:
            if needs_teardown:
                try:
                    await asyncio.wait_for(
                        self.write_next(relay, writer),
                        timeout=_TEARDOWN_WRITE_TIMEOUT_SECONDS,
                    )
                except (BrowserProtocolError, TimeoutError):
                    pass
        finally:
            # Cancellation can land while write_next is waiting behind the
            # operation writer's lock, during the teardown write, or after it
            # returns. The relay's one-way closing -> closed transition must not
            # depend on the close coordinator surviving any of those awaits.
            with self._lock:
                if relay.state != "closed":
                    self._finalize_close_locked(relay, status=relay._close_status or status)
        return changed

    async def cancel_operation(
        self, operation: BrowserOperation, *, writer: BrowserFrameWriter
    ) -> bool:
        return await self._close_with_writer(
            operation.relay, status="browser_cancelled", writer=writer
        )

    async def timeout_operation(
        self, operation: BrowserOperation, *, writer: BrowserFrameWriter
    ) -> bool:
        return await self._close_with_writer(
            operation.relay, status="browser_timed_out", writer=writer
        )

    def due_operations(self, context: BrowserContext) -> tuple[BrowserOperation, ...]:
        """Return one expired live operation per relay using the manager clock."""

        now = self._monotonic()
        with self._lock:
            due: list[BrowserOperation] = []
            for relay in tuple(context.relays):
                if relay.state != "active":
                    continue
                operation = next(
                    (
                        candidate
                        for candidate in relay._operations.values()
                        if not candidate.terminal and candidate.deadline_at <= now
                    ),
                    None,
                )
                if operation is not None:
                    due.append(operation)
            return tuple(due)

    def request_consumer_disconnect(self, relay: RelayBinding) -> bool:
        """Queue exact-relay cancellation for the browser-socket pump to deliver."""

        with self._lock:
            return self._begin_close_locked(
                relay, status="browser_cancelled", queue_teardown=True
            )

    async def interrupt_operation(
        self, operation: BrowserOperation, *, writer: BrowserFrameWriter
    ) -> bool:
        return await self._close_with_writer(
            operation.relay, status="browser_cancelled", writer=writer
        )

    def accept_operation_response(
        self,
        relay: RelayBinding,
        *,
        operation_id: Any,
        payload: Any,
        transport: object,
        sid: str,
        profile: str,
        capability_generation: int,
        relay_token: str,
        binding_generation: int,
        failed: bool = False,
    ) -> bool:
        """Fence, validate, and settle one terminal response exactly once."""

        with self._lock:
            self.authorize_frame(
                relay,
                transport=transport,
                sid=sid,
                profile=profile,
                capability_generation=capability_generation,
                relay_token=relay_token,
                binding_generation=binding_generation,
            )
            # The exact owner fence above is deliberately complete before any
            # operation-id inspection or payload serialization/access.
            if type(operation_id) is not str or not _OPERATION_ID_RE.fullmatch(operation_id):
                error = BrowserProtocolError(
                    "browser_invalid_operation_id",
                    "operation id is invalid",
                    delivery="outcome_unknown",
                )
                self._close_relay_locked(relay, status=error.code)
                raise error
            if type(failed) is not bool:
                error = BrowserProtocolError(
                    "browser_invalid_frame",
                    "terminal failure flag must be a boolean",
                    delivery="outcome_unknown",
                )
                self._close_relay_locked(relay, status=error.code)
                raise error
            operation = relay._operations.get(operation_id)
            if operation is None:
                # A syntactically valid id outside the live manager-owned set is
                # late or duplicate. Discard it before touching its payload; it
                # must neither settle another operation nor become a protocol
                # oracle that tears down an otherwise healthy relay.
                return False
            if operation.request_id is not None:
                response_id = payload.get("id") if isinstance(payload, Mapping) else None
                if type(response_id) is not type(operation.request_id) or response_id != operation.request_id:
                    error = BrowserProtocolError(
                        "browser_response_id_mismatch",
                        "CDP response id does not own this operation",
                        delivery="outcome_unknown",
                    )
                    self._close_relay_locked(relay, status=error.code)
                    raise error
            if operation.state == "writing":
                # The authenticated matching response proves the peer received
                # the command even if the local writer coroutine has not yet
                # resumed to report acceptance. Freeze the terminal response;
                # write_next observes and preserves it when its await returns.
                operation.state = "dispatched"
            try:
                # Enforce the reverse-direction cap before accepting a terminal frame.
                _serialize_frame(payload, limit=self._limits.application_message_bytes)
            except BrowserProtocolError as exc:
                exc.delivery = "outcome_unknown"
                self._close_relay_locked(relay, status=exc.code)
                raise
            if operation.state != "dispatched":
                raise BrowserProtocolError(
                    "browser_operation_not_dispatched", "operation was not dispatched"
                )
            return self._settle_operation_locked(
                operation,
                status="failed" if failed else "completed",
                retryable=False,
                delivery="confirmed",
            )

    def _close_relay_locked(self, relay: RelayBinding, *, status: str) -> bool:
        changed = self._begin_close_locked(relay, status=status, queue_teardown=False)
        self._finalize_close_locked(relay, status=relay._close_status or status)
        return changed

    def close_relay(self, relay: RelayBinding, *, status: str = "browser_cancelled") -> bool:
        """Immediately close when no live writer is available.

        Cancellation, timeout, and interrupt use ``_close_with_writer`` and send
        their reserved teardown through ``write_next``. This transport-loss path
        deliberately performs no teardown write because its caller has no writer.
        """

        with self._lock:
            return self._close_relay_locked(relay, status=status)

    def authorize_frame(
        self,
        relay: RelayBinding,
        *,
        transport: object,
        sid: str,
        profile: str,
        capability_generation: int,
        relay_token: str,
        binding_generation: int,
    ) -> None:
        """Apply the exact D-015 five-field fence, then the tab-binding epoch.

        The five owner fields are transport identity, browser sid, profile,
        capability generation, and relay token.  Binding generation is a
        distinct task/tab lifecycle guard; it does not replace or widen that
        owner tuple.
        """

        with self._lock:
            context = relay.context
            checks = (
                ("transport", transport is context.transport),
                (
                    "sid",
                    type(sid) is str and sid == relay.sid and context.sid == relay.sid,
                ),
                (
                    "profile",
                    type(profile) is str
                    and profile == relay.profile
                    and context.profile == relay.profile,
                ),
                (
                    "capability_generation",
                    type(capability_generation) is int
                    and capability_generation == relay.capability_generation
                    and context.capability_generation == relay.capability_generation,
                ),
                (
                    "relay_token",
                    type(relay_token) is str
                    and hmac.compare_digest(relay_token.encode(), relay.relay_token.encode()),
                ),
            )
            for field_name, accepted in checks:
                if not accepted:
                    raise BrowserProtocolError(
                        f"browser_fence_{field_name}", f"{field_name} does not own this relay"
                    )
            if (
                not relay.active
                or type(binding_generation) is not int
                or binding_generation != relay.binding_generation
                or context.binding_generation != relay.binding_generation
            ):
                raise BrowserProtocolError(
                    "browser_stale_binding_generation", "binding generation is no longer live"
                )

    async def _transition(self, context: BrowserContext, status: str) -> bool:
        self.close_context_adapters(context, status=status)
        with self._lock:
            if context.state in {"closed", "revoked", "killed", "disabled"}:
                return False
            self._invalidate_relays(context, status=status)
            self._retire_sid_locked(context.sid)
            context.state = {
                "browser_killed": "killed",
                "browser_disabled": "disabled",
            }.get(status, "revoked")
            generation = context.capability_generation
        payload: dict[str, Any] = {
            "type": "browser.outcome",
            "status": status,
            "retryable": False,
            "delivery": "outcome_unknown",
        }
        if generation is not None:
            payload["capability_generation"] = generation
        try:
            await context.transport.send_json(payload)
        except Exception:
            pass
        try:
            await context.transport.close(code=4410, reason=status)
        except Exception:
            pass
        return True

    async def disable_context(self, context: BrowserContext) -> bool:
        """Disable one Desktop's browser lease without touching its chat association."""

        return await self._transition(context, "browser_disabled")

    async def revoke(self, *, principal: str, profile: str, reason: str = "browser_revoked") -> int:
        with self._lock:
            contexts = [
                context
                for context in self._contexts
                if context.principal == principal and context.ticket_profile == profile
            ]
        results = [await self._transition(context, reason) for context in contexts]
        return sum(results)

    async def revoke_principal(self, *, principal: str, reason: str = "browser_revoked") -> int:
        """Revoke every browser lease for one logged-out credential principal."""

        with self._lock:
            contexts = [context for context in self._contexts if context.principal == principal]
        results = [await self._transition(context, reason) for context in contexts]
        return sum(results)

    async def kill(self, *, principal: str, profile: str) -> int:
        with self._lock:
            contexts = [
                context
                for context in self._contexts
                if context.principal == principal and context.ticket_profile == profile
            ]
        results = [await self._transition(context, "browser_killed") for context in contexts]
        return sum(results)

    async def disable(self, *, profile: str) -> int:
        with self._lock:
            contexts = [context for context in self._contexts if context.ticket_profile == profile]
        results = [await self._transition(context, "browser_disabled") for context in contexts]
        return sum(results)

    def disconnect(self, context: BrowserContext) -> None:
        self.close_context_adapters(context, status="browser_disconnected")
        with self._lock:
            self._invalidate_relays(context, status="browser_disconnected")
            self._retire_sid_locked(context.sid)
            context.state = "closed"
            self._contexts = [row for row in self._contexts if row is not context]

    def redacted_diagnostics(self) -> dict[str, Any]:
        """Return payload/token/identity-free bounded transport diagnostics."""

        with self._lock:
            contexts: list[dict[str, Any]] = []
            for context in self._contexts:
                relay_rows: list[dict[str, Any]] = []
                for relay in context.relays:
                    relay_rows.append(
                        {
                            "state": relay.state,
                            "target_bound": True,
                            "capability_generation": relay.capability_generation,
                            "binding_generation": relay.binding_generation,
                            "outstanding": relay.outstanding_count,
                            "outbound": relay.outbound.diagnostics(),
                            "inbound": relay.inbound.diagnostics(),
                        }
                    )
                contexts.append(
                    {
                        "state": context.state,
                        "present": context.present,
                        "capability_generation": context.capability_generation,
                        "binding_generation": context.binding_generation,
                        "relays": relay_rows,
                    }
                )
            return {
                "association_count": len(self._chat),
                "ready_count": sum(1 for context in self._contexts if context.present),
                "contexts": contexts,
            }


class BrowserRelayDuplex:
    """Socketless OnePageRelay adapter backed by one manager-owned relay."""

    def __init__(self, manager: BrowserTransportManager, relay: RelayBinding) -> None:
        self._manager = manager
        self._relay = relay

    async def send(self, raw: str) -> None:
        validate_application_message_size(raw, limit=self._manager._limits.application_message_bytes)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise BrowserProtocolError("browser_invalid_frame", "relay command is invalid JSON") from exc
        if not isinstance(payload, Mapping) or type(payload.get("method")) is not str:
            raise BrowserProtocolError("browser_invalid_frame", "relay command must be a CDP request")
        if payload.get("id") is None:
            raise BrowserProtocolError("browser_invalid_frame", "relay command id is required")
        self._manager.admit_operation(self._relay, payload)

    def consumer_disconnected(self) -> bool:
        return self._manager.request_consumer_disconnect(self._relay)

    async def __aiter__(self):
        while True:
            frame = self._manager.dequeue_inbound(self._relay)
            if frame is not None:
                yield frame.serialized
                continue
            if self._relay.state == "closed":
                return
            await asyncio.sleep(0.01)


def get_browser_transport_manager(application: Any) -> BrowserTransportManager:
    """Return the manager scoped to one FastAPI application/listener."""

    manager = getattr(application.state, "browser_transport_manager", None)
    if manager is None:
        manager = BrowserTransportManager()
        application.state.browser_transport_manager = manager
    return manager


def browser_sid_is_non_resumable(sid: str) -> bool:
    """Reject browser routing ids before chat session/database resolution.

    Managers are weakly registered so test/application lifetimes do not create a
    process-global leak. Each live manager retains a bounded five-minute set of
    retired ids, preventing a just-disconnected browser sid from falling through
    into the durable chat-session namespace.
    """

    if type(sid) is not str or not sid:
        return False
    with _browser_managers_lock:
        managers = tuple(_browser_managers)
    return any(manager.owns_or_recently_retired_sid(sid) for manager in managers)
