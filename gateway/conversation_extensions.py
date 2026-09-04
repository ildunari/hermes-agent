"""Generic gateway conversation-extension contracts.

This module is the *only* seam a plugin needs to participate in gateway
conversation routing, turn augmentation, tool authorization, and lifecycle.
It is deliberately provider-neutral: nothing here knows about any particular
plugin, contact, relationship, or product policy.

Design constraints:

* **One atomic bundle.** An extension registers a single immutable
  :class:`GatewayConversationExtension` containing every subinterface it
  implements. Partial per-phase replacement is not permitted, so a reload can
  never leave half of one generation's policy live beside half of another's.
* **Generation-scoped unload.** Every registration returns a monotonic
  generation. Unload only succeeds for the generation that is currently
  active, so a slow teardown of an old generation cannot clear a newer one.
* **Profile isolation.** Registrations are keyed by resolved Hermes home. An
  extension loaded in one profile is invisible to every other profile.
* **Fail closed on policy, fail open on enrichment.** Routing, admission, and
  tool authorization deny on any error, malformed result, or missing
  extension. Optional recall/observation degrade silently so a plugin bug
  cannot take down ordinary replies.
* **Bounded host surface.** Extensions receive :class:`GatewayRuntimeFacade`,
  never a ``GatewayRunner``, session store, SDK client, credential, or private
  callback.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from agent.request_scoped_tools import (
    RequestScopedTool,
    record_current_request_scoped_usage,
)

logger = logging.getLogger(__name__)


# The version a plugin must declare to be accepted by this core build.
EXTENSION_API_VERSION = 1


# Every capability an extension may declare, mapped to the bundle field that
# must be supplied when it is declared. Declaring a capability without its
# callback (or supplying a callback without declaring the capability) is a
# contract error caught at registration time rather than at fire time.
CAPABILITY_FIELDS: dict[str, str] = {
    "admission_policy": "authorize_route",
    "turn_policy": "augment_turn",
    "tool_authorization": "authorize_tool",
    "ingress_observer": "observe_ingress",
    "post_turn_observer": "observe_turn_result",
    "lifecycle": "on_start",
    "health": "health",
}

KNOWN_CAPABILITIES = frozenset(CAPABILITY_FIELDS)

# Capabilities the facade requires before exposing each bounded host action.
_FACADE_CAPABILITY_REQUIREMENTS = {
    "spawn_lifecycle_task": "lifecycle",
    "load_initiated_turn_context": "initiated_turns",
    "create_initiated_child": "initiated_turns",
    "inject_turn": "turn_injection",
    "probe_authenticated_existing_dm": "authenticated_dm",
    "send_authenticated_existing_dm": "authenticated_dm",
    "call_auxiliary_model": "auxiliary_model",
    "web_search": "web_research",
    "list_cron_jobs": "health",
}

# Capabilities that are host-granted rather than callback-backed. A plugin
# declares them to request a bounded host action; there is no matching
# bundle field because the host owns the implementation.
_HOST_GRANTED_CAPABILITIES = frozenset(
    {
        "initiated_turns",
        "turn_injection",
        "authenticated_dm",
        "auxiliary_model",
        "web_research",
    }
)

ALL_CAPABILITIES = KNOWN_CAPABILITIES | _HOST_GRANTED_CAPABILITIES


class CapabilityDenied(RuntimeError):
    """Raised when an extension uses a host action it did not declare."""


# ---------------------------------------------------------------------------
# Route / admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayRouteContext:
    """Trusted, pre-authorization description of one inbound message.

    Every field is captured by core from the adapter before any extension
    runs. ``transport_profile`` / ``transport_home`` identify the trust domain
    that authenticated the sender and are immutable for the life of the
    request — an extension may propose a *runtime* route, never a new
    transport identity.

    ``ingress_records`` carries the adapter's frozen, transport-authenticated
    per-message records for this event. It is what lets an extension that owns
    the ingress domain actually *persist* the arrival rather than count it: an
    owner with no access to the records could only ever be a no-op, which is a
    zero-owner outage dressed up as a handover. The records are opaque to core
    — it captures and forwards them without interpreting their contents.
    """

    platform: str
    adapter_identity: str
    transport_profile: str
    transport_home: str
    sender_identity: str
    chat_id: str
    chat_type: str
    text_preview: str = ""
    is_group: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    ingress_records: tuple[Any, ...] = ()
    raw_message: Mapping[str, Any] | None = None
    message_id: str = ""
    text: str = ""


@dataclass(frozen=True)
class GatewayRouteDirective:
    """An extension's *proposal* for admitting and routing one message.

    ``runtime_profile`` is validated by core against served profiles and the
    permitted route map before it takes effect.

    ``principal`` / ``subject_id`` let an admission owner report a bounded,
    opaque identity classification. ``context_prefix`` / ``scope_metadata``
    carry extension-owned context without core interpreting product policy.
    """

    admit: bool
    runtime_profile: Optional[str] = None
    reason: str = ""
    tags: tuple[str, ...] = ()
    principal: Optional[str] = None
    subject_id: Optional[str] = None
    subject_display_name: Optional[str] = None
    context_prefix: str = ""
    scope_metadata: Mapping[str, Any] = field(default_factory=dict)
    suppress_turn: bool = False


@dataclass(frozen=True)
class GatewayRouteDecision:
    """Core's final, validated routing outcome."""

    admitted: bool
    transport_profile: str
    transport_home: str
    runtime_profile: str
    reason: str = ""
    extension_id: Optional[str] = None
    generation: Optional[int] = None
    principal: Optional[str] = None
    subject_id: Optional[str] = None
    subject_display_name: Optional[str] = None
    context_prefix: str = ""
    scope_metadata: Mapping[str, Any] = field(default_factory=dict)
    suppress_turn: bool = False


# ---------------------------------------------------------------------------
# Turn augmentation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayTurnContext:
    """Per-turn description handed to an extension for optional enrichment."""

    session_key: str
    runtime_profile: str
    platform: str
    sender_identity: str
    chat_type: str
    user_text: str
    profile_home: str = ""
    session_id: str = ""
    principal: str = ""
    subject_id: str = ""
    turn_index: int = 0
    now_timestamp: Optional[float] = None
    current_message_id: Optional[str] = None
    conversation_history: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayTurnAugmentation:
    """Optional per-turn contributions from an extension.

    ``degraded`` is set by core (never by the plugin) when the extension
    failed or returned something malformed, so the caller can record a health
    signal while still serving the reply.
    """

    user_context: tuple[str, ...] = ()
    system_context: tuple[str, ...] = ()
    request_tools: tuple[RequestScopedTool, ...] = ()
    on_success: tuple[Callable[[], Any], ...] = ()
    degraded: bool = False


@dataclass(frozen=True)
class GatewayTurnResult:
    """Authenticated post-turn completion record."""

    session_key: str
    runtime_profile: str
    platform: str
    sender_identity: str
    user_text: str
    assistant_text: str
    delivered: bool
    user_message_id: Optional[str] = None
    assistant_message_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool authorization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayToolAuthorizationRequest:
    """One final-dispatch tool authorization question."""

    function_name: str
    function_args: Mapping[str, Any]
    profile_home: str
    route_id: str


@dataclass(frozen=True)
class GatewayToolAuthorizationDecision:
    """An extension's allow/deny answer for one tool dispatch."""

    allowed: bool
    reason: str = ""
    requires_approval: bool = False


@dataclass(frozen=True)
class GatewayRequestPolicy:
    """Immutable request-scoped policy token issued by core after routing.

    Issued once per admitted request and propagated explicitly across every
    executor/thread hop. Its presence is what makes final-dispatch tool
    authorization mandatory for that request.
    """

    extension_id: str
    profile_home: str
    route_id: str


# ---------------------------------------------------------------------------
# Lifecycle / health / host capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayBackgroundTask:
    """A host-owned background task registration.

    ``identity`` deliberately includes the generation so replacing an
    extension cancels exactly the outgoing generation's tasks.
    """

    identity: tuple[str, str, str, int]
    factory: Callable[[], Any]

    @property
    def extension_id(self) -> str:
        return self.identity[0]

    @property
    def profile_home(self) -> str:
        return self.identity[1]

    @property
    def task_key(self) -> str:
        return self.identity[2]

    @property
    def generation(self) -> int:
        return self.identity[3]


@dataclass(frozen=True)
class GatewayExtensionHealth:
    """Bounded health answer from an extension."""

    healthy: bool
    detail: str = ""


@dataclass(frozen=True)
class InitiatedTurnRequest:
    """Generic request to create an initiated-assistant child turn."""

    session_key: str
    prompt: str
    origin: str
    parent_session_key: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InitiatedTurnContext:
    """Immutable parent context for extension-owned initiated composition."""

    parent_session_id: str
    session_key: str
    system_prompt: str
    history: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class AuxiliaryModelRequest:
    """Strict no-fallback auxiliary inference requested by an extension."""

    task: str
    provider: str
    model: str
    messages: tuple[Mapping[str, str], ...]
    reasoning_effort: str
    max_tokens: int = 500


@dataclass(frozen=True)
class WebSearchRequest:
    """Bounded ordinary web search requested by an extension."""

    query: str
    limit: int = 5


class DmSendOutcome(str, Enum):
    """Tri-state result of an authenticated existing-DM send."""

    SENT = "sent"
    DEFINITIVE_FAILURE = "definitive_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthenticatedDmRequest:
    """Send to an *existing* authenticated DM.

    There is intentionally no ``create_if_missing`` and no fallback target:
    if the chat does not already exist and is not already authorized, the
    action fails definitively. ``reservation_key`` must be durably reserved by
    the caller *before* this request is constructed so a retry after an
    ``UNKNOWN`` outcome is idempotent at the transport layer.
    """

    platform: str
    chat_id: str
    text: str
    reservation_key: str
    expected_participants: tuple[str, ...] = ()
    expected_route_fingerprint: str = ""
    profile_name: str = ""
    session_id: str = ""

    def __post_init__(self) -> None:
        for name in ("platform", "chat_id", "text", "reservation_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")


@dataclass(frozen=True)
class AuthenticatedDmProbeRequest:
    """Read-only proof that one configured participant still owns a live DM."""

    platform: str
    chat_id: str
    expected_participants: tuple[str, ...]
    profile_name: str = ""
    session_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.platform, str) or not self.platform.strip():
            raise ValueError("platform is required")
        if not isinstance(self.chat_id, str) or not self.chat_id.strip():
            raise ValueError("chat_id is required")
        if not self.expected_participants or any(
            not isinstance(value, str) or not value.strip()
            for value in self.expected_participants
        ):
            raise ValueError("expected_participants is required")


@dataclass(frozen=True)
class AuthenticatedDmProbeResult:
    """Bounded readiness result; no adapter, GUID, or credential escapes core."""

    adapter_ready: bool
    authorized_existing_dm: bool
    participant_match: bool
    detail: str = ""
    route_fingerprint: Optional[str] = None


@dataclass(frozen=True)
class AuthenticatedDmResult:
    """Outcome plus optional transport receipt."""

    outcome: DmSendOutcome
    receipt: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class GatewayHostOperations:
    """Host-supplied implementations wired by the gateway at start.

    Every field is optional so core, tests, and a no-gateway CLI process can
    all construct a facade. A missing implementation denies rather than
    silently succeeding.
    """

    spawn_task: Optional[Callable[[GatewayBackgroundTask], Any]] = None
    cancel_tasks: Optional[Callable[[str, str, int], Any]] = None
    lookup_session: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None
    load_initiated_turn_context: Optional[
        Callable[[str, str, str], Optional[InitiatedTurnContext]]
    ] = None
    create_initiated_child: Optional[Callable[[InitiatedTurnRequest], Mapping[str, Any]]] = None
    inject_turn: Optional[Callable[[str, str], bool]] = None
    send_authenticated_existing_dm: Optional[
        Callable[[AuthenticatedDmRequest], Any]
    ] = None
    probe_authenticated_existing_dm: Optional[
        Callable[[AuthenticatedDmProbeRequest], Any]
    ] = None
    call_auxiliary_model: Optional[Callable[[AuxiliaryModelRequest], str]] = None
    web_search: Optional[Callable[[WebSearchRequest], Any]] = None
    list_cron_jobs: Optional[
        Callable[[str, bool], tuple[Mapping[str, Any], ...]]
    ] = None
    #: Run a blocking callable off the gateway event loop. An owner of a
    #: durable domain necessarily performs SQLite work, and doing it inline
    #: would stall every other conversation. The host owns the thread pool;
    #: the extension only supplies the callable.
    run_blocking: Optional[Callable[..., Any]] = None


class GatewayRuntimeFacade:
    """Bounded host capabilities handed to one extension generation.

    Deliberately exposes no ``runner``, ``gateway``, ``session_store``,
    ``adapter``, ``client``, ``credentials``, or ``config`` attribute. Each
    action is gated on the capability the extension declared at registration.
    """

    __slots__ = (
        "_extension_id",
        "_profile_name",
        "_profile_home",
        "_generation",
        "_capabilities",
        "_host",
    )

    def __init__(
        self,
        *,
        extension_id: str,
        profile_name: str,
        profile_home: str,
        generation: int,
        capabilities: frozenset[str],
        host: GatewayHostOperations,
    ) -> None:
        self._extension_id = extension_id
        self._profile_name = profile_name
        self._profile_home = profile_home
        self._generation = generation
        self._capabilities = frozenset(capabilities)
        self._host = host

    # -- identity ----------------------------------------------------------

    @property
    def extension_id(self) -> str:
        return self._extension_id

    @property
    def profile_name(self) -> str:
        return self._profile_name

    @property
    def profile_home(self) -> str:
        return self._profile_home

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def _require(self, action: str) -> None:
        needed = _FACADE_CAPABILITY_REQUIREMENTS.get(action)
        if needed and needed not in self._capabilities:
            raise CapabilityDenied(
                f"extension '{self._extension_id}' did not declare the "
                f"'{needed}' capability required for {action}()"
            )

    def _operation(self, name: str) -> Any:
        """Return the newest installed host operation, then the captured stub.

        Extensions may register during executor-backed MCP discovery, before
        ``GatewayRunner.start`` installs the complete host. Their facade must
        upgrade when that host arrives; pinning non-lifecycle methods to the
        early stub leaves a correctly started watcher unable to model, probe,
        persist, or deliver until the next process restart.
        """
        live = gateway_host_operations()
        return getattr(live, name, None) or getattr(self._host, name, None)

    # -- bounded actions ---------------------------------------------------

    def spawn_lifecycle_task(
        self, task_key: str, factory: Callable[[], Any]
    ) -> GatewayBackgroundTask:
        """Register a host-owned background task for this generation.

        Fails closed when the host provides no task runner: an extension must
        never believe it started a watcher that nothing is running.
        """
        self._require("spawn_lifecycle_task")
        if not isinstance(task_key, str) or not task_key.strip():
            raise ValueError("task_key is required")
        if not callable(factory):
            raise ValueError("factory must be callable")
        live = gateway_host_operations()
        spawn = live.spawn_task or self._host.spawn_task
        if spawn is None:
            raise CapabilityDenied(
                "host does not provide lifecycle task scheduling; "
                "refusing to report a task as started"
            )
        task = GatewayBackgroundTask(
            identity=(
                self._extension_id,
                self._profile_home,
                task_key,
                self._generation,
            ),
            factory=factory,
        )
        spawn(task)
        return task

    def lookup_session(self, session_key: str) -> Optional[Mapping[str, Any]]:
        """Return an immutable session snapshot, never a live store handle."""
        lookup = self._operation("lookup_session")
        if lookup is None:
            return None
        try:
            snapshot = lookup(session_key)
        except Exception:
            logger.debug("extension session lookup failed", exc_info=True)
            return None
        return dict(snapshot) if isinstance(snapshot, Mapping) else None

    def create_initiated_child(self, request: InitiatedTurnRequest) -> Mapping[str, Any]:
        self._require("create_initiated_child")
        if not isinstance(request, InitiatedTurnRequest):
            raise ValueError("request must be an InitiatedTurnRequest")
        create = self._operation("create_initiated_child")
        if create is None:
            raise CapabilityDenied("host does not provide initiated child creation")
        return create(request)

    def load_initiated_turn_context(
        self, parent_session_id: str
    ) -> Optional[InitiatedTurnContext]:
        """Return a copied parent transcript for initiated-turn composition."""
        self._require("load_initiated_turn_context")
        if not isinstance(parent_session_id, str) or not parent_session_id.strip():
            raise ValueError("parent_session_id is required")
        loader = self._operation("load_initiated_turn_context")
        if loader is None:
            raise CapabilityDenied(
                "host does not provide initiated-turn context loading"
            )
        context = loader(self._profile_home, self._profile_name, parent_session_id)
        return context if isinstance(context, InitiatedTurnContext) else None

    def call_auxiliary_model(self, request: AuxiliaryModelRequest) -> str:
        """Run one strict host-routed auxiliary request with no fallback."""
        self._require("call_auxiliary_model")
        if not isinstance(request, AuxiliaryModelRequest):
            raise ValueError("request must be an AuxiliaryModelRequest")
        caller = self._operation("call_auxiliary_model")
        if caller is None:
            raise CapabilityDenied("host does not provide auxiliary model calls")
        return str(caller(request) or "")

    def web_search(self, request: WebSearchRequest) -> Any:
        """Run one bounded host-owned ordinary web search."""
        self._require("web_search")
        if not isinstance(request, WebSearchRequest):
            raise ValueError("request must be a WebSearchRequest")
        search = self._operation("web_search")
        if search is None:
            raise CapabilityDenied("host does not provide web research")
        return search(request)

    def list_cron_jobs(self, *, include_disabled: bool) -> tuple:
        """Return copied cron metadata for health inspection only."""
        self._require("list_cron_jobs")
        reader = self._operation("list_cron_jobs")
        if reader is None:
            raise CapabilityDenied("host does not provide cron inspection")
        return tuple(reader(self._profile_home, bool(include_disabled)) or ())

    def inject_turn(self, *, session_key: str, text: str) -> bool:
        self._require("inject_turn")
        inject = self._operation("inject_turn")
        if inject is None:
            raise CapabilityDenied("host does not provide turn injection")
        return bool(inject(session_key, text))

    def send_authenticated_existing_dm(
        self, request: AuthenticatedDmRequest
    ) -> Any:
        """Send to an existing authenticated DM; never creates or falls back."""
        self._require("send_authenticated_existing_dm")
        if not isinstance(request, AuthenticatedDmRequest):
            raise ValueError("request must be an AuthenticatedDmRequest")
        send = self._operation("send_authenticated_existing_dm")
        if send is None:
            return AuthenticatedDmResult(
                DmSendOutcome.DEFINITIVE_FAILURE, detail="capability_unavailable"
            )
        return send(request)

    def probe_authenticated_existing_dm(
        self, request: AuthenticatedDmProbeRequest
    ) -> Any:
        """Read-only adapter, session-auth, and participant-identity proof."""
        self._require("probe_authenticated_existing_dm")
        if not isinstance(request, AuthenticatedDmProbeRequest):
            raise ValueError("request must be an AuthenticatedDmProbeRequest")
        probe = self._operation("probe_authenticated_existing_dm")
        if probe is None:
            return AuthenticatedDmProbeResult(False, False, False, "capability_unavailable")
        return probe(request)

    def run_blocking(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run *func* on the host's worker pool, off the event loop.

        Returns an awaitable when the host provides one. Falls back to running
        inline only when no host implementation exists (CLI, tests), which is
        correct there because those callers have no event loop to protect.
        """
        run = self._operation("run_blocking")
        if run is None:
            return func(*args, **kwargs)
        return run(func, *args, **kwargs)

    def describe(self) -> dict[str, Any]:
        """Bounded status snapshot: identity and capabilities only."""
        return {
            "extension_id": self._extension_id,
            "profile": self._profile_name,
            "generation": self._generation,
            "capabilities": sorted(self._capabilities),
        }


# ---------------------------------------------------------------------------
# The extension bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayConversationExtension:
    """One atomically registered, generation-scoped extension bundle."""

    extension_id: str
    api_version: int
    capabilities: frozenset[str]

    authorize_route: Optional[Callable[[GatewayRouteContext], GatewayRouteDirective]] = None
    augment_turn: Optional[Callable[[GatewayTurnContext], GatewayTurnAugmentation]] = None
    authorize_tool: Optional[
        Callable[[GatewayToolAuthorizationRequest], GatewayToolAuthorizationDecision]
    ] = None
    observe_ingress: Optional[Callable[[GatewayRouteContext], None]] = None
    observe_turn_result: Optional[Callable[[GatewayTurnResult], None]] = None
    on_start: Optional[Callable[[GatewayRuntimeFacade], None]] = None
    on_stop: Optional[Callable[[GatewayRuntimeFacade], None]] = None
    health: Optional[Callable[[], GatewayExtensionHealth]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.extension_id, str) or not self.extension_id.strip():
            raise ValueError("extension_id is required")
        if self.api_version != EXTENSION_API_VERSION:
            raise ValueError(
                f"extension '{self.extension_id}' declares api_version "
                f"{self.api_version}; this core supports {EXTENSION_API_VERSION}"
            )
        if not isinstance(self.capabilities, frozenset):
            object.__setattr__(self, "capabilities", frozenset(self.capabilities or ()))
        unknown = self.capabilities - ALL_CAPABILITIES
        if unknown:
            raise ValueError(
                f"extension '{self.extension_id}' declares unknown capabilities: "
                f"{sorted(unknown)}"
            )
        # Declared capability without its callback, or callback without its
        # declaration — both are contract errors, caught here rather than at
        # fire time where the safe response would be a silent deny.
        for capability, attribute in CAPABILITY_FIELDS.items():
            declared = capability in self.capabilities
            supplied = getattr(self, attribute) is not None
            if declared and not supplied:
                raise ValueError(
                    f"extension '{self.extension_id}' declares '{capability}' "
                    f"but supplied no {attribute} callback"
                )
            if supplied and not declared:
                raise ValueError(
                    f"extension '{self.extension_id}' supplied {attribute} "
                    f"without declaring the '{capability}' capability"
                )
        for attribute in (*CAPABILITY_FIELDS.values(), "on_stop"):
            value = getattr(self, attribute)
            if value is not None and not callable(value):
                raise ValueError(f"{attribute} must be callable")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ConversationExtensionRegistry:
    """Profile-scoped registry of active extension generations.

    Registration is atomic under its own lock and hands back a monotonic
    generation id. Unload is a compare-and-clear against that generation, so
    a late teardown of an outgoing generation can never remove a newer one.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counter = itertools.count(1)
        # scope -> extension_id -> (generation, bundle)
        self._active: dict[str, dict[str, tuple[int, GatewayConversationExtension]]] = {}

    def register(
        self, extension: GatewayConversationExtension, *, scope: str
    ) -> int:
        generation = self.reserve_generation(extension, scope=scope)
        if not self.publish(
            extension,
            generation=generation,
            scope=scope,
            expected_previous_generation=self.active_generation(
                extension.extension_id, scope=scope
            ),
        ):
            raise RuntimeError("conversation extension registration raced")
        return generation

    def reserve_generation(
        self, extension: GatewayConversationExtension, *, scope: str
    ) -> int:
        """Reserve a monotonic generation without making it observable."""
        if not isinstance(extension, GatewayConversationExtension):
            raise ValueError("extension must be a GatewayConversationExtension")
        if not isinstance(scope, str) or not scope:
            raise ValueError("scope is required")
        with self._lock:
            return next(self._counter)

    def publish(
        self,
        extension: GatewayConversationExtension,
        *,
        generation: int,
        scope: str,
        expected_previous_generation: Optional[int],
    ) -> bool:
        """Atomically publish a started generation if the predecessor is unchanged."""
        if not isinstance(extension, GatewayConversationExtension):
            raise ValueError("extension must be a GatewayConversationExtension")
        if not isinstance(scope, str) or not scope:
            raise ValueError("scope is required")
        with self._lock:
            current = self._active.get(scope, {}).get(extension.extension_id)
            current_generation = current[0] if current else None
            if current_generation != expected_previous_generation:
                return False
            self._active.setdefault(scope, {})[extension.extension_id] = (
                generation,
                extension,
            )
            return True

    def unregister(
        self, extension_id: str, *, generation: int, scope: str
    ) -> bool:
        """Remove *extension_id* only if *generation* is still the active one."""
        with self._lock:
            entries = self._active.get(scope)
            if not entries:
                return False
            current = entries.get(extension_id)
            if current is None or current[0] != generation:
                return False
            entries.pop(extension_id, None)
            if not entries:
                self._active.pop(scope, None)
            return True

    def get(
        self, extension_id: str, *, scope: str
    ) -> Optional[GatewayConversationExtension]:
        with self._lock:
            entry = self._active.get(scope, {}).get(extension_id)
            return entry[1] if entry else None

    def active_generation(self, extension_id: str, *, scope: str) -> Optional[int]:
        with self._lock:
            entry = self._active.get(scope, {}).get(extension_id)
            return entry[0] if entry else None

    def active_ids(self, *, scope: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active.get(scope, {})))

    def snapshot(
        self, *, scope: str
    ) -> tuple[tuple[str, int, GatewayConversationExtension], ...]:
        with self._lock:
            return tuple(
                (extension_id, generation, bundle)
                for extension_id, (generation, bundle) in sorted(
                    self._active.get(scope, {}).items()
                )
            )

    def reset_for_tests(self) -> None:
        with self._lock:
            self._active.clear()


conversation_extension_registry = ConversationExtensionRegistry()


class _HostOperationsRegistry:
    """Process-wide holder for the host implementations behind the facade.

    The gateway installs real implementations at start; everything else (CLI,
    tests, a no-gateway process) sees the empty default, where every effectful
    capability denies rather than silently succeeding.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._operations = GatewayHostOperations()

    def install(self, operations: GatewayHostOperations) -> GatewayHostOperations:
        if not isinstance(operations, GatewayHostOperations):
            raise ValueError("operations must be GatewayHostOperations")
        with self._lock:
            previous = self._operations
            self._operations = operations
            return previous

    def get(self) -> GatewayHostOperations:
        with self._lock:
            return self._operations

    def reset(self) -> None:
        with self._lock:
            self._operations = GatewayHostOperations()


_host_operations = _HostOperationsRegistry()


def gateway_host_operations() -> GatewayHostOperations:
    """Return the host operations currently installed by the gateway."""
    return _host_operations.get()


def install_gateway_host_operations(
    operations: GatewayHostOperations,
) -> GatewayHostOperations:
    """Install host operations; returns the previous set for restoration."""
    return _host_operations.install(operations)


def reset_gateway_host_operations() -> None:
    _host_operations.reset()


class LifecycleTaskRegistry:
    """Tracks host-owned extension tasks keyed by full generation identity.

    Cancellation is always scoped to ``(extension_id, profile_home,
    generation)``, which is what makes a stale generation's teardown unable to
    touch a newer generation's live tasks.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[tuple[str, str, int], list[tuple[str, Any]]] = {}

    def record(self, task: GatewayBackgroundTask, handle: Any) -> None:
        key = (task.extension_id, task.profile_home, task.generation)
        with self._lock:
            self._tasks.setdefault(key, []).append((task.task_key, handle))

    def cancel(self, extension_id: str, profile_home: str, generation: int) -> int:
        """Cancel every task for exactly this generation. Returns the count."""
        key = (extension_id, profile_home, generation)
        with self._lock:
            entries = self._tasks.pop(key, [])
        cancelled = 0
        for task_key, handle in entries:
            cancel = getattr(handle, "cancel", None)
            if not callable(cancel):
                continue
            try:
                cancel()
                cancelled += 1
            except Exception:
                logger.debug(
                    "failed to cancel extension task %s/%s", extension_id, task_key,
                    exc_info=True,
                )
        return cancelled

    def active_generations(self, extension_id: str, profile_home: str) -> tuple[int, ...]:
        with self._lock:
            return tuple(
                sorted(
                    generation
                    for (ext, home, generation) in self._tasks
                    if ext == extension_id and home == profile_home
                )
            )

    def reset_for_tests(self) -> None:
        with self._lock:
            self._tasks.clear()


lifecycle_task_registry = LifecycleTaskRegistry()


def _current_scope(scope: Optional[str]) -> str:
    if scope:
        return scope
    from hermes_constants import hermes_home_key

    return hermes_home_key()


# ---------------------------------------------------------------------------
# Requirements and readiness (core-owned, fail closed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequiredExtension:
    """A profile's core-owned declaration that an extension must be present."""

    extension_id: str
    api_version: int
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExtensionCheck:
    extension_id: str
    ok: bool
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ExtensionReadinessReport:
    ready: bool
    checks: tuple[ExtensionCheck, ...] = ()

    @property
    def status(self) -> str:
        return "ok" if self.ready else "unready"

    def as_dict(self) -> dict[str, Any]:
        """Bounded readiness payload: statuses and reasons, never messages."""
        return {
            "status": self.status,
            "extensions": [
                {
                    "id": check.extension_id,
                    "status": "ok" if check.ok else "unready",
                    **({"reason": check.reason} if check.reason else {}),
                }
                for check in self.checks
            ],
        }


def parse_required_extensions(raw: Any) -> tuple[RequiredExtension, ...]:
    """Parse ``gateway.required_conversation_extensions`` from config.

    Malformed declarations raise rather than being dropped: a profile that
    *tried* to require an extension must never silently start without one.
    """
    if raw in (None, "", ()):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError("required_conversation_extensions must be a list")
    parsed: list[RequiredExtension] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("each required extension must be a mapping")
        extension_id = item.get("id")
        if not isinstance(extension_id, str) or not extension_id.strip():
            raise ValueError("required extension entry is missing 'id'")
        api_version = item.get("api_version", EXTENSION_API_VERSION)
        if not isinstance(api_version, int) or isinstance(api_version, bool):
            raise ValueError(f"required extension '{extension_id}' has a non-integer api_version")
        capabilities = item.get("capabilities") or ()
        if not isinstance(capabilities, (list, tuple)):
            raise ValueError(f"required extension '{extension_id}' capabilities must be a list")
        unknown = set(capabilities) - ALL_CAPABILITIES
        if unknown:
            raise ValueError(
                f"required extension '{extension_id}' names unknown capabilities: {sorted(unknown)}"
            )
        parsed.append(
            RequiredExtension(
                extension_id=extension_id.strip(),
                api_version=api_version,
                capabilities=frozenset(capabilities),
            )
        )
    return tuple(parsed)


def evaluate_extension_readiness(
    *,
    required: Sequence[RequiredExtension],
    scope: Optional[str] = None,
) -> ExtensionReadinessReport:
    """Fail-closed readiness for a profile's required extensions.

    A profile with no requirements is always ready — ordinary Hermes profiles
    keep normal no-extension behavior.
    """
    if not required:
        return ExtensionReadinessReport(ready=True)

    active_scope = _current_scope(scope)
    checks: list[ExtensionCheck] = []
    for requirement in required:
        bundle = conversation_extension_registry.get(
            requirement.extension_id, scope=active_scope
        )
        if bundle is None:
            checks.append(ExtensionCheck(requirement.extension_id, False, "missing"))
            continue
        if bundle.api_version != requirement.api_version:
            checks.append(
                ExtensionCheck(requirement.extension_id, False, "api_version_mismatch")
            )
            continue
        missing = requirement.capabilities - bundle.capabilities
        if missing:
            checks.append(
                ExtensionCheck(requirement.extension_id, False, "missing_capability")
            )
            continue
        if bundle.health is not None:
            try:
                health = bundle.health()
            except Exception:
                logger.debug(
                    "required extension %s health probe raised",
                    requirement.extension_id,
                    exc_info=True,
                )
                checks.append(
                    ExtensionCheck(requirement.extension_id, False, "health_error")
                )
                continue
            if not isinstance(health, GatewayExtensionHealth):
                checks.append(
                    ExtensionCheck(requirement.extension_id, False, "malformed_health")
                )
                continue
            if not health.healthy:
                checks.append(
                    ExtensionCheck(requirement.extension_id, False, "unhealthy")
                )
                continue
        checks.append(ExtensionCheck(requirement.extension_id, True))

    return ExtensionReadinessReport(
        ready=all(check.ok for check in checks), checks=tuple(checks)
    )


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------


def resolve_route(
    context: GatewayRouteContext,
    *,
    scope: Optional[str] = None,
    served_profiles: Sequence[str] = (),
    permitted_routes: Mapping[str, Sequence[str]] | None = None,
) -> GatewayRouteDecision:
    """Run the immutable transport-home / admission / runtime-route sequence.

    Core has already captured the trusted adapter identity in *context*. Any
    registered admission extension returns a typed proposal; core validates
    the proposed runtime profile against the served set and the permitted
    route map before it is allowed to take effect. The transport trust domain
    never moves.
    """
    active_scope = _current_scope(scope)
    permitted = permitted_routes or {}
    baseline = GatewayRouteDecision(
        admitted=True,
        transport_profile=context.transport_profile,
        transport_home=context.transport_home,
        runtime_profile=context.transport_profile,
    )

    entries = [
        (extension_id, generation, bundle)
        for extension_id, generation, bundle in conversation_extension_registry.snapshot(
            scope=active_scope
        )
        if bundle.authorize_route is not None
    ]
    if not entries:
        return baseline

    # One admission owner per profile keeps the decision unambiguous; more
    # than one is a configuration error and must fail closed.
    if len(entries) > 1:
        return GatewayRouteDecision(
            admitted=False,
            transport_profile=context.transport_profile,
            transport_home=context.transport_home,
            runtime_profile=context.transport_profile,
            reason="ambiguous_admission_owner",
        )

    extension_id, generation, bundle = entries[0]
    try:
        directive = bundle.authorize_route(context)  # type: ignore[misc]
    except Exception:
        logger.warning(
            "conversation extension %s raised during route authorization; denying",
            extension_id,
            exc_info=True,
        )
        return GatewayRouteDecision(
            admitted=False,
            transport_profile=context.transport_profile,
            transport_home=context.transport_home,
            runtime_profile=context.transport_profile,
            reason="extension_error",
            extension_id=extension_id,
            generation=generation,
        )

    if not isinstance(directive, GatewayRouteDirective):
        return GatewayRouteDecision(
            admitted=False,
            transport_profile=context.transport_profile,
            transport_home=context.transport_home,
            runtime_profile=context.transport_profile,
            reason="malformed_directive",
            extension_id=extension_id,
            generation=generation,
        )

    if not directive.admit:
        return GatewayRouteDecision(
            admitted=False,
            transport_profile=context.transport_profile,
            transport_home=context.transport_home,
            runtime_profile=context.transport_profile,
            reason=directive.reason or "denied",
            extension_id=extension_id,
            generation=generation,
        )

    target = directive.runtime_profile or context.transport_profile
    if target != context.transport_profile:
        if target not in set(served_profiles):
            return GatewayRouteDecision(
                admitted=False,
                transport_profile=context.transport_profile,
                transport_home=context.transport_home,
                runtime_profile=context.transport_profile,
                reason="route_not_served",
                extension_id=extension_id,
                generation=generation,
            )
        allowed = set(permitted.get(context.transport_profile, ()))
        if target not in allowed:
            return GatewayRouteDecision(
                admitted=False,
                transport_profile=context.transport_profile,
                transport_home=context.transport_home,
                runtime_profile=context.transport_profile,
                reason="route_not_permitted",
                extension_id=extension_id,
                generation=generation,
            )

    return GatewayRouteDecision(
        admitted=True,
        transport_profile=context.transport_profile,
        transport_home=context.transport_home,
        runtime_profile=target,
        reason=directive.reason,
        extension_id=extension_id,
        generation=generation,
        # Identity classification performed by the admission owner. Core keeps
        # it opaque but bounded; product-specific values remain in the plugin.
        principal=(
            directive.principal.strip()[:128]
            if isinstance(directive.principal, str) and directive.principal.strip()
            else None
        ),
        subject_id=(
            str(directive.subject_id) if directive.subject_id else None
        ),
        subject_display_name=(
            str(directive.subject_display_name)
            if directive.subject_display_name
            else None
        ),
        context_prefix=(
            directive.context_prefix
            if isinstance(directive.context_prefix, str)
            else ""
        ),
        scope_metadata=(
            dict(directive.scope_metadata)
            if isinstance(directive.scope_metadata, Mapping)
            else {}
        ),
        suppress_turn=bool(directive.suppress_turn),
    )


# ---------------------------------------------------------------------------
# Turn augmentation (optional enrichment: fail open)
# ---------------------------------------------------------------------------


def collect_turn_augmentation(
    context: GatewayTurnContext,
    *,
    scope: Optional[str] = None,
    extension_id: Optional[str] = None,
) -> GatewayTurnAugmentation:
    """Collect optional per-turn context. Never raises into the turn."""
    active_scope = _current_scope(scope)
    user: list[str] = []
    system: list[str] = []
    tools: list[RequestScopedTool] = []
    success_callbacks: list[Callable[[], Any]] = []
    degraded = False

    for registered_id, _generation, bundle in conversation_extension_registry.snapshot(
        scope=active_scope
    ):
        if extension_id is not None and registered_id != extension_id:
            continue
        if bundle.augment_turn is None:
            continue
        try:
            augmentation = bundle.augment_turn(context)
        except Exception:
            logger.warning(
                "conversation extension %s raised during turn augmentation; continuing",
                registered_id,
                exc_info=True,
            )
            degraded = True
            continue
        if not isinstance(augmentation, GatewayTurnAugmentation):
            logger.warning(
                "conversation extension %s returned a malformed turn augmentation",
                registered_id,
            )
            degraded = True
            continue
        user.extend(_clean_strings(augmentation.user_context))
        system.extend(_clean_strings(augmentation.system_context))
        clean_tools, tools_degraded = _clean_request_tools(augmentation.request_tools)
        clean_callbacks, callbacks_degraded = _clean_callbacks(augmentation.on_success)
        tools.extend(clean_tools)
        success_callbacks.extend(clean_callbacks)
        degraded = degraded or tools_degraded or callbacks_degraded

    return GatewayTurnAugmentation(
        user_context=tuple(user),
        system_context=tuple(system),
        request_tools=tuple(tools),
        on_success=tuple(success_callbacks),
        degraded=degraded,
    )


def _clean_strings(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [value for value in values if isinstance(value, str) and value.strip()]


def _clean_request_tools(
    values: Any,
) -> tuple[list[RequestScopedTool], bool]:
    """Return executable request tools and flag malformed contributions.

    Enrichment remains fail-open, but executable objects are never silently
    converted to names or discarded. A malformed contribution marks the
    augmentation degraded and is omitted before it reaches the binding layer.
    """
    if not isinstance(values, (list, tuple)):
        return [], values not in (None, ())
    clean: list[RequestScopedTool] = []
    names: set[str] = set()
    degraded = False
    for value in values:
        if not isinstance(value, RequestScopedTool):
            degraded = True
            continue
        try:
            name = value.name
        except (TypeError, ValueError):
            degraded = True
            continue
        if not callable(value.handler) or (
            value.on_success is not None and not callable(value.on_success)
        ):
            degraded = True
            continue
        if name in names:
            degraded = True
            continue
        names.add(name)
        clean.append(value)
    return clean, degraded


def _clean_callbacks(values: Any) -> tuple[list[Callable[[], Any]], bool]:
    if not isinstance(values, (list, tuple)):
        return [], values not in (None, ())
    clean = [value for value in values if callable(value)]
    return clean, len(clean) != len(values)


# ---------------------------------------------------------------------------
# Observation fire sites (never raise into the gateway)
# ---------------------------------------------------------------------------


def notify_ingress(
    context: GatewayRouteContext, *, scope: Optional[str] = None
) -> None:
    """Authenticated-ingress observation fire site."""
    _notify("observe_ingress", context, scope=scope)


def notify_turn_result(
    result: GatewayTurnResult, *, scope: Optional[str] = None
) -> None:
    """Post-turn completion fire site with trusted route identity."""
    _notify("observe_turn_result", result, scope=scope)


def _notify(attribute: str, payload: Any, *, scope: Optional[str]) -> None:
    active_scope = _current_scope(scope)
    for extension_id, _generation, bundle in conversation_extension_registry.snapshot(
        scope=active_scope
    ):
        callback = getattr(bundle, attribute, None)
        if callback is None:
            continue
        try:
            callback(payload)
        except Exception:
            logger.warning(
                "conversation extension %s raised in %s; ignoring",
                extension_id,
                attribute,
                exc_info=True,
            )


def notify_gateway_start(
    facades: Iterable[GatewayRuntimeFacade], *, scope: Optional[str] = None
) -> None:
    """Gateway-start lifecycle fire site."""
    _notify_lifecycle("on_start", facades, scope=scope)


def notify_gateway_stop(
    facades: Iterable[GatewayRuntimeFacade], *, scope: Optional[str] = None
) -> None:
    """Gateway-stop lifecycle fire site."""
    _notify_lifecycle("on_stop", facades, scope=scope)


def _notify_lifecycle(
    attribute: str, facades: Iterable[GatewayRuntimeFacade], *, scope: Optional[str]
) -> None:
    active_scope = _current_scope(scope)
    for facade in facades:
        bundle = conversation_extension_registry.get(
            facade.extension_id, scope=active_scope
        )
        if bundle is None:
            continue
        callback = getattr(bundle, attribute, None)
        if callback is None:
            continue
        try:
            callback(facade)
        except Exception:
            logger.warning(
                "conversation extension %s raised in %s; ignoring",
                facade.extension_id,
                attribute,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Request policy + final-dispatch tool authorization
# ---------------------------------------------------------------------------


_REQUEST_POLICY: ContextVar[Optional[GatewayRequestPolicy]] = ContextVar(
    "hermes_gateway_request_policy", default=None
)


def issue_request_policy(
    *, extension_id: str, profile_home: str, route_id: str
) -> GatewayRequestPolicy:
    """Mint the immutable request-policy token after validated routing."""
    if not extension_id or not profile_home or not route_id:
        raise ValueError("extension_id, profile_home, and route_id are required")
    return GatewayRequestPolicy(
        extension_id=extension_id, profile_home=profile_home, route_id=route_id
    )


@contextmanager
def request_policy_scope(policy: Optional[GatewayRequestPolicy]):
    """Bind *policy* for the duration of one request."""
    token = _REQUEST_POLICY.set(policy)
    try:
        yield policy
    finally:
        _REQUEST_POLICY.reset(token)


def current_request_policy() -> Optional[GatewayRequestPolicy]:
    return _REQUEST_POLICY.get()


def reset_request_policy_for_tests() -> None:
    _REQUEST_POLICY.set(None)


def _deny(reason: str, *, requires_approval: bool = False) -> str:
    return json.dumps(
        {
            "error": reason,
            "requires_approval": requires_approval,
            "extension_policy": True,
        },
        ensure_ascii=False,
    )


def authorize_tool_dispatch(
    function_name: str, function_args: Mapping[str, Any] | None
) -> Optional[str]:
    """Final-dispatch tool authorization for every execution path.

    Returns ``None`` when the call is allowed and a JSON denial string when it
    is not. When no request policy is bound (ordinary non-extension traffic)
    this is a no-op, preserving normal behavior with no extension registered.

    When a policy *is* bound, the decision is mandatory: a vanished extension,
    a raising callback, or a malformed decision all deny.
    """
    policy = _REQUEST_POLICY.get()
    if policy is None:
        return None

    bundle = conversation_extension_registry.get(
        policy.extension_id, scope=policy.profile_home
    )
    if bundle is None or bundle.authorize_tool is None:
        logger.warning(
            "request policy references unavailable extension %s; denying %s",
            policy.extension_id,
            function_name,
        )
        return _deny("policy_unavailable")

    request = GatewayToolAuthorizationRequest(
        function_name=function_name,
        function_args=dict(function_args or {}),
        profile_home=policy.profile_home,
        route_id=policy.route_id,
    )
    try:
        decision = bundle.authorize_tool(request)
    except Exception:
        logger.warning(
            "conversation extension %s raised during tool authorization; denying %s",
            policy.extension_id,
            function_name,
            exc_info=True,
        )
        return _deny("tool policy guard failed closed")

    if not isinstance(decision, GatewayToolAuthorizationDecision):
        logger.warning(
            "conversation extension %s returned a malformed tool decision; denying %s",
            policy.extension_id,
            function_name,
        )
        return _deny("tool policy guard failed closed")

    if decision.allowed:
        return None
    return _deny(
        decision.reason or f"{function_name} is not permitted for this request",
        requires_approval=bool(decision.requires_approval),
    )


__all__ = [
    "ALL_CAPABILITIES",
    "AuthenticatedDmRequest",
    "AuthenticatedDmProbeRequest",
    "AuthenticatedDmProbeResult",
    "AuthenticatedDmResult",
    "AuxiliaryModelRequest",
    "CAPABILITY_FIELDS",
    "CapabilityDenied",
    "ConversationExtensionRegistry",
    "DmSendOutcome",
    "EXTENSION_API_VERSION",
    "ExtensionCheck",
    "ExtensionReadinessReport",
    "GatewayBackgroundTask",
    "GatewayConversationExtension",
    "GatewayExtensionHealth",
    "GatewayHostOperations",
    "GatewayRequestPolicy",
    "GatewayRouteContext",
    "GatewayRouteDecision",
    "GatewayRouteDirective",
    "GatewayRuntimeFacade",
    "GatewayToolAuthorizationDecision",
    "GatewayToolAuthorizationRequest",
    "GatewayTurnAugmentation",
    "GatewayTurnContext",
    "GatewayTurnResult",
    "InitiatedTurnRequest",
    "InitiatedTurnContext",
    "WebSearchRequest",
    "KNOWN_CAPABILITIES",
    "LifecycleTaskRegistry",
    "RequiredExtension",
    "RequestScopedTool",
    "authorize_tool_dispatch",
    "collect_turn_augmentation",
    "conversation_extension_registry",
    "current_request_policy",
    "evaluate_extension_readiness",
    "gateway_host_operations",
    "install_gateway_host_operations",
    "issue_request_policy",
    "lifecycle_task_registry",
    "notify_gateway_start",
    "notify_gateway_stop",
    "notify_ingress",
    "notify_turn_result",
    "parse_required_extensions",
    "request_policy_scope",
    "record_current_request_scoped_usage",
    "reset_gateway_host_operations",
    "reset_request_policy_for_tests",
    "resolve_route",
]
