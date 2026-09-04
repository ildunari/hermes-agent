"""GatewayRunner host implementation for conversation extensions.

This module is local gateway infrastructure.  The public plugin contract remains
in ``gateway.conversation_extensions`` and its runtime companion.
"""

import asyncio
import dataclasses
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from gateway.config import Platform

logger = logging.getLogger(__name__)

_early_host_ready: asyncio.Event | None = None
_early_host_loop: asyncio.AbstractEventLoop | None = None


def _mark_full_host_ready() -> None:
    """Release extension factories queued during pre-start plugin discovery."""
    event = _early_host_ready
    loop = _early_host_loop
    if event is None or loop is None or loop.is_closed():
        return
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is loop:
        event.set()
    else:
        loop.call_soon_threadsafe(event.set)



def install_early_lifecycle_scheduling() -> None:
    """Install spawn_task as soon as a running loop exists.

    Multiplex profile plugins (poke/guest) register conversation extensions
    during GatewayRunner construction / MCP warmup — several seconds before
    ``GatewayRunner.start`` logs "Starting Hermes Gateway..." and installs
    the full host. Without spawn_task, poke ``on_start`` fails closed
    (``CapabilityDenied``) and later ``fire_gateway_start`` skips duplicate.

    The full host (session lookup, authenticated DM, turn injection) is
    installed later in ``_install_conversation_extension_host``. This early
    install only provides lifecycle scheduling so watchers can start.
    """
    from gateway.conversation_extensions import (
        GatewayHostOperations,
        gateway_host_operations,
        install_gateway_host_operations,
        lifecycle_task_registry,
    )

    global _early_host_loop, _early_host_ready

    existing = gateway_host_operations()
    if existing.spawn_task is not None:
        return

    # MCP discovery is intentionally blocking and runs in an executor.  A
    # plugin discovered there still registers its conversation extension from
    # that worker thread.  Capture the gateway's owning loop now so lifecycle
    # factories can always be marshalled back to it instead of relying on the
    # registration thread having a running loop.
    owner_loop = asyncio.get_running_loop()
    host_ready = asyncio.Event()
    _early_host_loop = owner_loop
    _early_host_ready = host_ready

    def _spawn(task) -> None:
        async def _run_factory():
            await host_ready.wait()
            return await _coerce_awaitable(task.factory())

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is owner_loop:
            handle = owner_loop.create_task(
                _run_factory(),
                name=f"ext:{task.extension_id}:{task.task_key}:{task.generation}",
            )
        else:
            handle = asyncio.run_coroutine_threadsafe(_run_factory(), owner_loop)
        lifecycle_task_registry.record(task, handle)

    def _coerce_awaitable(value):
        if asyncio.iscoroutine(value):
            return value

        async def _wrap():
            return value

        return _wrap()

    install_gateway_host_operations(
        GatewayHostOperations(
            spawn_task=_spawn,
            cancel_tasks=lifecycle_task_registry.cancel,
            lookup_session=existing.lookup_session,
            load_initiated_turn_context=existing.load_initiated_turn_context,
            create_initiated_child=existing.create_initiated_child,
            inject_turn=existing.inject_turn,
            send_authenticated_existing_dm=existing.send_authenticated_existing_dm,
            probe_authenticated_existing_dm=existing.probe_authenticated_existing_dm,
            call_auxiliary_model=existing.call_auxiliary_model,
            web_search=existing.web_search,
            list_cron_jobs=existing.list_cron_jobs,
            run_blocking=existing.run_blocking or asyncio.to_thread,
        )
    )


def _install_conversation_extension_host(self) -> None:
    """Install the bounded host operations behind ``GatewayRuntimeFacade``.

    Only these narrow capabilities are exposed. Notably absent: the
    ``GatewayRunner`` itself, mutable session stores, raw platform/SDK
    clients, credentials, and any private callback.
    """
    from gateway.conversation_extensions import (
        GatewayHostOperations,
        install_gateway_host_operations,
        lifecycle_task_registry,
    )

    def _spawn(task) -> None:
        # Host owns the task object; the extension only ever holds the
        # immutable descriptor. Cancellation is keyed by full generation
        # identity so a stale teardown cannot touch a newer generation.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "No running loop; refusing to start extension task %s",
                task.task_key,
            )
            raise
        handle = loop.create_task(
            _coerce_awaitable(task.factory()),
            name=f"ext:{task.extension_id}:{task.task_key}:{task.generation}",
        )
        lifecycle_task_registry.record(task, handle)

    def _coerce_awaitable(value):
        if asyncio.iscoroutine(value):
            return value

        async def _wrap():
            return value

        return _wrap()

    def _run_blocking(func, *args, **kwargs):
        """Run a blocking extension callable on the host's thread pool.

        An extension that owns a durable domain must do SQLite work, and
        doing it on the gateway loop would stall every conversation. The
        host owns the pool; the extension supplies only the callable.
        Returns an awaitable, matching ``asyncio.to_thread``.
        """
        return asyncio.to_thread(func, *args, **kwargs)

    def _lookup_session(session_key: str):
        """Return an immutable snapshot, never the live store.

        Reads under the store's own lock and copies out only two scalar
        fields; the extension never receives the entry object or the store.
        """
        if not session_key:
            return None
        try:
            store = self.session_store
            with store._lock:  # noqa: SLF001 — read-only snapshot
                store._ensure_loaded_locked()  # noqa: SLF001
                entry = store._entries.get(session_key)  # noqa: SLF001
                if entry is None:
                    return None
                return {
                    "session_id": getattr(entry, "session_id", None),
                    "profile": getattr(entry, "profile", None),
                }
        except Exception:
            logger.debug("extension session lookup failed", exc_info=True)
            return None

    def _session_db_for_parent(parent_session_id: str):
        """Resolve the sync DB that actually owns a multiplexed parent."""
        candidates = []
        try:
            current = getattr(self, "_session_db", None)
            candidates.append(getattr(current, "_db", current))
        except Exception:
            pass
        handles = getattr(self, "_session_db_handles", None)
        lock = getattr(self, "_session_db_handles_lock", None)
        try:
            if lock is not None:
                with lock:
                    candidates.extend(
                        getattr(value, "_db", value)
                        for value in (handles or {}).values()
                    )
            else:
                candidates.extend(
                    getattr(value, "_db", value)
                    for value in (handles or {}).values()
                )
        except Exception:
            logger.debug("could not snapshot session DB handles", exc_info=True)
        seen = set()
        for db in candidates:
            if db is None or id(db) in seen:
                continue
            seen.add(id(db))
            try:
                if db.get_session(parent_session_id) is not None:
                    return db
            except Exception:
                continue
        return None

    def _load_initiated_turn_context(
        profile_home: str, profile_name: str, parent_session_id: str
    ):
        """Copy the minimum parent state needed for plugin-owned composition."""
        from gateway.conversation_extensions import InitiatedTurnContext

        db = _session_db_for_parent(parent_session_id)
        if db is None:
            return None
        try:
            parent = db.get_session(parent_session_id)
            messages = db.get_messages(parent_session_id)
        except Exception:
            logger.debug("initiated-turn context read failed", exc_info=True)
            return None
        if not isinstance(parent, Mapping):
            return None
        parent_profile = str(parent.get("profile_name") or parent.get("profile") or "")
        if parent_profile and parent_profile != str(profile_name):
            logger.warning(
                "refusing cross-profile initiated context read for %s",
                profile_name,
            )
            return None
        session_key = str(parent.get("session_key") or "").strip()
        if not session_key:
            return None
        history = []
        for message in messages or ():
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "")
            content = message.get("content")
            if role not in {"user", "assistant", "tool"} or not isinstance(content, str):
                continue
            history.append({"role": role, "content": content})
        return InitiatedTurnContext(
            parent_session_id=parent_session_id,
            session_key=session_key,
            system_prompt=str(parent.get("system_prompt") or ""),
            history=tuple(history),
        )

    def _create_initiated_child(request) -> Dict[str, Any]:
        """Create a bounded agent-initiated child turn for an existing session.

        Deliberately narrow. The extension supplies a session key and the
        assistant content; core resolves the *existing* session itself and
        refuses if there is none. Nothing here creates a chat, invents a
        recipient, or hands back a store handle — the return value is a
        plain dict of scalars.
        """
        from gateway.conversation_extensions import InitiatedTurnRequest

        if not isinstance(request, InitiatedTurnRequest):
            return {"created": False, "reason": "malformed_request"}
        prompt = str(request.prompt or "").strip()
        if not prompt:
            return {"created": False, "reason": "empty_content"}

        try:
            entry = self.session_store.lookup_by_session_key(request.session_key)
        except Exception:
            logger.debug(
                "extension initiated-child session lookup failed", exc_info=True
            )
            return {"created": False, "reason": "session_lookup_failed"}
        if entry is None:
            # No existing session: refuse rather than fabricate a lineage.
            return {"created": False, "reason": "unknown_session"}

        parent_session_id = getattr(entry, "session_id", None)
        if not parent_session_id:
            return {"created": False, "reason": "unknown_session"}

        session_db = _session_db_for_parent(str(parent_session_id))
        if session_db is None:
            return {"created": False, "reason": "session_db_unavailable"}

        child_session_id = f"ext-{request.origin or 'extension'}-{uuid.uuid4().hex}"
        try:
            session_db.create_initiated_assistant_child(
                parent_session_id=str(parent_session_id),
                child_session_id=child_session_id,
                assistant_content=prompt,
                initiated_kind="checkin",
            )
        except Exception as exc:
            logger.warning(
                "extension initiated-child creation failed: %s", exc, exc_info=True
            )
            return {"created": False, "reason": "creation_failed"}
        return {
            "created": True,
            "session_id": child_session_id,
            "parent_session_id": str(parent_session_id),
        }

    def _inject_turn(session_key: str, text: str) -> bool:
        """Schedule a safe, authorization-checked turn on an existing session.

        Reuses the existing plugin message-injection path, which re-runs
        ``_is_user_authorized`` against the session's stored origin and
        refuses when the gateway is not running or is draining. The
        injected event is marked internal with ``allow_gateway_control``
        off, so an extension cannot drive gateway control commands.
        """
        if not isinstance(session_key, str) or not session_key.strip():
            return False
        content = str(text or "").strip()
        if not content:
            return False
        try:
            return bool(
                self._schedule_plugin_message_injection(
                    session_key=session_key,
                    content=content,
                    plugin_id="conversation_extension",
                )
            )
        except Exception:
            logger.warning("extension turn injection failed", exc_info=True)
            return False

    def _resolve_authenticated_dm_transport(platform_name: str):
        """Resolve the live adapter for exactly this platform. No fallback."""
        try:
            platform = Platform(platform_name)
        except Exception:
            return None
        adapter = (getattr(self, "adapters", None) or {}).get(platform)
        if adapter is not None:
            return adapter
        for profile_map in (getattr(self, "_profile_adapters", None) or {}).values():
            candidate = (profile_map or {}).get(platform)
            if candidate is not None:
                return candidate
        return None

    def _is_authorized_existing_dm(platform_name: str, chat_id: str) -> bool:
        """True only for a DM that still exists in the authorized session set."""
        try:
            platform = Platform(platform_name)
        except Exception:
            return False
        try:
            store = self.session_store
            with store._lock:  # noqa: SLF001 — read-only scan
                store._ensure_loaded_locked()  # noqa: SLF001
                entries = list(store._entries.values())  # noqa: SLF001
        except Exception:
            logger.debug("authenticated DM authorization scan failed", exc_info=True)
            return False

        for entry in entries:
            origin = getattr(entry, "origin", None)
            if origin is None:
                continue
            if getattr(origin, "platform", None) != platform:
                continue
            if str(getattr(origin, "chat_id", "") or "") != str(chat_id):
                continue
            if str(getattr(origin, "chat_type", "") or "") != "dm":
                continue
            try:
                return bool(
                    self._is_user_authorized(origin, allow_adapter_delegation=False)
                )
            except Exception:
                logger.debug(
                    "authenticated DM authorization check failed", exc_info=True
                )
                return False
        return False

    async def _probe_authenticated_existing_dm(request):
        from gateway.conversation_extensions import (
            AuthenticatedDmProbeRequest,
            AuthenticatedDmProbeResult,
        )

        if not isinstance(request, AuthenticatedDmProbeRequest):
            raise ValueError("malformed authenticated DM probe request")
        adapter = _resolve_authenticated_dm_transport(request.platform)
        authorized = _is_authorized_existing_dm(request.platform, request.chat_id)
        if adapter is None:
            return AuthenticatedDmProbeResult(False, authorized, False, "transport_unavailable")
        resolver = getattr(adapter, "resolve_authenticated_existing_dm", None)
        if not callable(resolver):
            return AuthenticatedDmProbeResult(True, authorized, False, "participant_probe_unavailable")
        try:
            matched = resolver(request.chat_id, frozenset(request.expected_participants))
            if asyncio.iscoroutine(matched):
                matched = await matched
        except Exception:
            logger.debug("authenticated DM participant probe failed", exc_info=True)
            return AuthenticatedDmProbeResult(True, authorized, False, "participant_probe_failed")
        fingerprint = (
            str(matched[1]).strip()
            if isinstance(matched, (tuple, list)) and len(matched) >= 2 and matched[1]
            else None
        )
        return AuthenticatedDmProbeResult(
            True,
            authorized,
            bool(matched and fingerprint),
            "ready" if authorized and matched else "participant_or_session_mismatch",
            fingerprint,
        )

    async def _send_authenticated_existing_dm(request):
        """Send to an existing, already-authorized DM. Never creates, never falls back.

        The extension supplies only ``(platform, chat_id, text,
        reservation_key)``. It never receives an adapter, a client, or a
        credential: core resolves the transport itself and verifies the
        target is an existing authorized DM before sending.
        """
        from gateway.authenticated_dm import send_authenticated_existing_dm_async
        from gateway.conversation_extensions import (
            AuthenticatedDmResult,
            DmSendOutcome,
            AuthenticatedDmProbeRequest,
        )

        try:
            if not request.expected_participants or not request.expected_route_fingerprint:
                return AuthenticatedDmResult(
                    DmSendOutcome.DEFINITIVE_FAILURE,
                    detail="participant_proof_required",
                )
            probe = await _probe_authenticated_existing_dm(
                AuthenticatedDmProbeRequest(
                    platform=request.platform,
                    chat_id=request.chat_id,
                    expected_participants=tuple(request.expected_participants),
                )
            )
            if not (
                probe.adapter_ready
                and probe.authorized_existing_dm
                and probe.participant_match
                and probe.route_fingerprint == request.expected_route_fingerprint
            ):
                return AuthenticatedDmResult(
                    DmSendOutcome.DEFINITIVE_FAILURE,
                    detail="participant_or_route_mismatch",
                )
            return await send_authenticated_existing_dm_async(
                request,
                resolve_transport=_resolve_authenticated_dm_transport,
                is_authorized_existing_dm=_is_authorized_existing_dm,
            )
        except Exception:
            # An unclassifiable failure is UNKNOWN, never a definitive
            # failure: a wrong definitive verdict invites a duplicate send.
            logger.warning("authenticated DM send raised in host op", exc_info=True)
            return AuthenticatedDmResult(
                DmSendOutcome.UNKNOWN, detail="host_error"
            )

    def _call_auxiliary_model(request) -> str:
        from agent.auxiliary_client import call_llm
        from gateway.conversation_extensions import AuxiliaryModelRequest

        if not isinstance(request, AuxiliaryModelRequest):
            raise ValueError("malformed auxiliary model request")
        response = call_llm(
            task=request.task,
            provider=request.provider,
            model=request.model,
            messages=[dict(message) for message in request.messages],
            max_tokens=int(request.max_tokens),
            request_overrides={"reasoning_effort": request.reasoning_effort},
            allow_fallback=False,
        )
        if getattr(response, "_hermes_resolved_route", None) != {
            "provider": request.provider,
            "model": request.model,
        }:
            raise RuntimeError("resolved auxiliary route mismatch")
        choices = getattr(response, "choices", None) or []
        content = (
            getattr(getattr(choices[0], "message", None), "content", None)
            if choices
            else None
        )
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("empty auxiliary model response")
        return content.strip()

    def _web_search(request):
        from gateway.conversation_extensions import WebSearchRequest
        from tools.web_tools import web_search_tool

        if not isinstance(request, WebSearchRequest):
            raise ValueError("malformed web search request")
        return web_search_tool(request.query, limit=max(1, min(int(request.limit), 10)))

    def _list_cron_jobs(profile_home: str, include_disabled: bool):
        from cron.jobs import list_jobs, use_cron_store

        with use_cron_store(profile_home):
            jobs = list_jobs(include_disabled=include_disabled)
        return tuple(dict(job) for job in jobs if isinstance(job, Mapping))

    install_gateway_host_operations(
        GatewayHostOperations(
            spawn_task=_spawn,
            cancel_tasks=lifecycle_task_registry.cancel,
            lookup_session=_lookup_session,
            load_initiated_turn_context=_load_initiated_turn_context,
            create_initiated_child=_create_initiated_child,
            inject_turn=_inject_turn,
            send_authenticated_existing_dm=_send_authenticated_existing_dm,
            probe_authenticated_existing_dm=_probe_authenticated_existing_dm,
            call_auxiliary_model=_call_auxiliary_model,
            web_search=_web_search,
            list_cron_jobs=_list_cron_jobs,
            run_blocking=_run_blocking,
        )
    )

def _served_profile_names(self) -> tuple[str, ...]:
    """Return every profile this gateway process actually serves.

    Used to validate a conversation extension's proposed runtime route: a
    route to a profile this process does not serve is refused before it can
    enter any runtime scope.

    "Served" is a *configuration* property, not an adapter-connect outcome.
    Deriving it from ``_profile_adapters`` alone made a legitimate route
    fail as ``route_not_served`` whenever that profile's adapter happened
    to be down — coupling routing validity to transient connectivity. The
    set comes from the same ``_multiplex_profile_homes`` chokepoint
    ``_start_secondary_profile_adapters`` and profile-route validation use,
    so all three agree.
    """
    from gateway.run import _multiplex_profile_homes

    names: set[str] = set()
    try:
        from hermes_cli.profiles import get_active_profile_name

        names.add(get_active_profile_name() or "default")
    except Exception:
        names.add("default")
    # Config-declared served profiles, including any whose adapters failed
    # to connect. Single-profile gateways return just the active profile.
    try:
        config = getattr(self, "config", None)
        if getattr(config, "multiplex_profiles", False):
            for profile_name, _home in _multiplex_profile_homes(config):
                if isinstance(profile_name, str) and profile_name:
                    names.add(profile_name)
    except Exception:
        logger.debug(
            "could not enumerate config-declared served profiles", exc_info=True
        )
    # Union with live adapter profiles so a profile brought up by another
    # path is never dropped.
    try:
        for profile in (getattr(self, "_profile_adapters", None) or {}):
            if isinstance(profile, str) and profile:
                names.add(profile)
    except Exception:
        logger.debug("could not enumerate served profiles", exc_info=True)
    return tuple(sorted(names))


def _admission_scope_for_source(self, source, transport_home: str):
    """Return ``(scope, requirements_profile)`` for conversation-extension admission.

    Official multiplex routing only consults ``gateway.profile_routes``. Home
    has none, so inbound BlueBubbles stays on the default transport. Poke's
    classifier lives in the poke profile plugin, not home ``plugins.enabled``.
    Default-transport BlueBubbles therefore classifies against poke's
    registry so owner DMs can move default → poke and guests → guest.

    The transport profile/home on the route *context* stay default; only the
    registry scope changes. Do not enable a second BlueBubbles webhook.
    """
    from hermes_constants import hermes_home_key

    transport_scope = hermes_home_key(str(transport_home))
    if getattr(source, "profile", None):
        return transport_scope, getattr(source, "profile", None)
    platform = getattr(source, "platform", None)
    platform_value = getattr(platform, "value", platform)
    if str(platform_value or "").lower() != "bluebubbles":
        return transport_scope, getattr(source, "profile", None)
    try:
        from gateway.run import _multiplex_profile_homes

        for name, home in _multiplex_profile_homes(getattr(self, "config", None)):
            if name == "poke":
                return hermes_home_key(str(home)), "poke"
    except Exception:
        logger.debug("could not resolve poke admission scope", exc_info=True)
    return transport_scope, getattr(source, "profile", None)


def _permitted_extension_routes(self) -> Dict[str, tuple[str, ...]]:
    """Return the core-owned permitted runtime-route map.

    Shape: ``{transport_profile: (allowed_runtime_profile, ...)}``. Read
    from the ``gateway.permitted_conversation_routes`` config field, which
    ``GatewayConfig`` normalizes and validates. Absent or malformed config
    means *no* cross-profile routing is permitted — an extension can then
    only keep a message in its own transport profile, which is the
    fail-closed default.
    """
    try:
        raw = getattr(self.config, "permitted_conversation_routes", None)
        if not isinstance(raw, Mapping):
            return {}
        permitted: Dict[str, tuple[str, ...]] = {}
        for source_profile, targets in raw.items():
            if not isinstance(source_profile, str) or not source_profile:
                continue
            if isinstance(targets, str):
                targets = [targets]
            if not isinstance(targets, (list, tuple)):
                continue
            cleaned = tuple(
                target
                for target in targets
                if isinstance(target, str) and target
            )
            if cleaned:
                permitted[source_profile] = cleaned
        return permitted
    except Exception:
        logger.debug("could not read permitted conversation routes", exc_info=True)
        return {}

# -- conversation-extension lifecycle + eager activation ----------------

def _apply_extension_route_decision(self, source, event, decision):
    """Apply a validated extension routing directive to the source/event.

    This is what makes an admission extension the *real* routing owner
    rather than a rubber stamp. Without it the extension could pick a
    runtime profile that nothing acted on, and the opaque identity scope it
    reported would be discarded.

    Trust boundaries preserved here:

    * the **transport** profile/home are never changed — only the runtime
      profile, and only to the value core already validated against the
      served set and the permitted route map;
    * ``principal`` is carried as bounded opaque extension data; core never
      maps product-specific values to privileges;
    * the identity block is a *prefix*, never a replacement, and is not
      applied to command-shaped text (a leading ``/``), because
      ``get_command()`` only recognizes text whose first non-whitespace
      character is ``/`` — prefixing would turn owner control commands into
      model text.

    Returns ``(source, event)``, or ``(source, None)`` when the directive
    asks to suppress the turn.
    """
    if decision is None or not getattr(decision, "admitted", False):
        return source, event
    if getattr(decision, "suppress_turn", False):
        return source, None

    runtime_profile = str(getattr(decision, "runtime_profile", "") or "")
    principal = str(getattr(decision, "principal", "") or "").strip()
    subject_id = str(getattr(decision, "subject_id", "") or "").strip()
    extension_id = str(getattr(decision, "extension_id", "") or "").strip()

    # ``resolve_route`` returns an admitted baseline even when no extension
    # owns admission. That baseline is deliberately behavior-neutral: it
    # must not stamp ``profile=default`` or any trust marker onto ordinary
    # no-plugin traffic.
    if not extension_id:
        return source, event

    if not runtime_profile and not principal:
        return source, event

    if runtime_profile and runtime_profile != getattr(source, "profile", None):
        marker_subject = subject_id or str(getattr(source, "user_id", "") or "")
        transport_adapter_ref = getattr(source, "_transport_adapter_ref", None)
        route_changes = {
            "profile": runtime_profile,
            "user_id_alt": (
                f"extension:{extension_id}:{marker_subject}"
                if extension_id and marker_subject
                else getattr(source, "user_id_alt", None)
            ),
            "chat_id_alt": f"hermes-profile:{runtime_profile}",
        }
        if "extension_route_admitted" in getattr(source, "__dataclass_fields__", {}):
            route_changes["extension_route_admitted"] = True
        source = dataclasses.replace(source, **route_changes)
        if "extension_route_admitted" not in route_changes:
            try:
                source.extension_route_admitted = True
            except Exception:
                pass
        if transport_adapter_ref is not None:
            source._transport_adapter_ref = transport_adapter_ref
    elif extension_id:
        if "extension_route_admitted" in getattr(source, "__dataclass_fields__", {}):
            source = dataclasses.replace(source, extension_route_admitted=True)

    metadata = dict(getattr(event, "metadata", None) or {})
    if principal and subject_id:
        metadata["_hermes_extension_identity"] = {
            "principal": principal,
            "subject_id": subject_id,
            "source_text": str(getattr(event, "text", "") or "")[:4000],
        }

    prefix = str(getattr(decision, "context_prefix", "") or "")
    text = str(getattr(event, "text", "") or "")
    text_is_command = text.lstrip().startswith("/")
    if prefix and not text_is_command and not getattr(event, "observed_only", False):
        event = dataclasses.replace(
            event, source=source, text=prefix + text, metadata=metadata
        )
    else:
        event = dataclasses.replace(event, source=source, metadata=metadata)
    return source, event

def _apply_extension_route_decision_safe(self, source, event, decision):
    """``_apply_extension_route_decision`` that never raises into routing."""
    try:
        return self._apply_extension_route_decision(source, event, decision)
    except Exception:
        logger.error(
            "Refusing inbound message: could not apply the extension's "
            "validated route directive",
            exc_info=True,
        )
        return source, None

def _extension_runtime_profile(
    self, context, decision
) -> str:
    """Return the *validated* runtime profile for observation fire sites.

    The whole admission sequence exists to produce
    ``decision.runtime_profile``; reporting the transport profile instead
    would silently misreport identity to the observer under any permitted
    cross-profile route.
    """
    if decision is not None:
        runtime_profile = getattr(decision, "runtime_profile", "")
        if isinstance(runtime_profile, str) and runtime_profile:
            return runtime_profile
    return str(getattr(context, "transport_profile", "") or "default")

def _fire_extension_gateway_start(
    self, *, scope: str, profile_name: str
) -> tuple[str, ...]:
    """Run the gateway-start lifecycle fire site for one profile scope.

    Idempotent per scope: a profile activated twice (e.g. a retry path)
    must not double-fire ``on_start`` and double-spawn watchers.
    """
    started = getattr(self, "_extension_lifecycle_started", None)
    if started is None:
        started = set()
        self._extension_lifecycle_started = started
    if scope in started:
        return ()
    try:
        from gateway import conversation_extension_runtime as _ce_runtime

        ids = _ce_runtime.fire_gateway_start(
            scope=scope, profile_name=profile_name
        )
    except Exception:
        logger.warning(
            "conversation extension gateway-start fire site failed for %s",
            profile_name,
            exc_info=True,
        )
        return ()
    # Record the scope even when nothing was registered: a later
    # registration fires on_start through the registration path itself, and
    # re-running the gateway-start site would double-start it.
    started.add(scope)
    if ids:
        logger.info(
            "Started %d conversation extension(s) for profile %s: %s",
            len(ids),
            profile_name,
            ", ".join(ids),
        )
    return ids

def _fire_extension_gateway_stop(self) -> None:
    """Run the gateway-stop lifecycle fire site for every started scope.

    Without this, a gateway shutdown left extension lifecycle tasks running
    through the extension's own contract — ``on_stop`` only fired on plugin
    *unload*, which a process shutdown does not perform.
    """
    started = getattr(self, "_extension_lifecycle_started", None)
    if not started:
        return
    profiles = getattr(self, "_extension_scope_profiles", None) or {}
    for scope in sorted(started):
        try:
            from gateway import conversation_extension_runtime as _ce_runtime

            _ce_runtime.fire_gateway_stop(
                scope=scope, profile_name=profiles.get(scope, "default")
            )
        except Exception:
            logger.debug(
                "conversation extension gateway-stop fire site failed for %s",
                scope,
                exc_info=True,
            )
    started.clear()

def _activate_conversation_extensions_for_profile(
    self, profile_name: str, profile_home: "Path"
) -> bool:
    """Eagerly enumerate, activate, and hard-gate one served profile.

    Runs at *startup*, before the profile's adapters serve traffic — not
    lazily on the first inbound message. Concretely:

    1. Discovery has already run under this profile's home, so the
       registry holds whatever that profile registered.
    2. Fire the gateway-start lifecycle site for the profile's scope.
    3. Evaluate the profile's ``required_conversation_extensions``
       declaration and record a hard ready/unready verdict.

    Returns the readiness verdict. A profile that declares a requirement it
    cannot satisfy is recorded unready and its ingress is refused by
    ``_extension_profile_is_ready`` — so it never serves a single message,
    rather than discovering the problem per-inbound-message.

    A profile with no requirements is always ready, so ordinary Hermes
    profiles are completely unaffected.
    """
    from gateway.run import _load_gateway_config_from_home
    from hermes_constants import hermes_home_key

    readiness = getattr(self, "_extension_profile_readiness", None)
    if readiness is None:
        readiness = {}
        self._extension_profile_readiness = readiness
    scope_profiles = getattr(self, "_extension_scope_profiles", None)
    if scope_profiles is None:
        scope_profiles = {}
        self._extension_scope_profiles = scope_profiles

    try:
        scope = hermes_home_key(profile_home)
    except Exception:
        logger.error(
            "Could not resolve extension scope for profile %s; refusing to "
            "mark it ready",
            profile_name,
            exc_info=True,
        )
        readiness[f"unresolved:{profile_name}"] = {
            "ready": False,
            "reason": "scope_unresolvable",
        }
        return False
    scope_profiles[scope] = profile_name

    self._fire_extension_gateway_start(scope=scope, profile_name=profile_name)

    try:
        from gateway import conversation_extension_runtime as _ce_runtime

        # Read the requirement declaration from the profile home we were
        # given, not by re-resolving the profile *name*. The home is the
        # authoritative identity here (it is what the scope key is derived
        # from), and name-based resolution would read a different file for
        # any profile whose home is not the conventional
        # ``~/.hermes/profiles/<name>``.
        config_raw = _load_gateway_config_from_home(Path(profile_home))
        ok, reason = _ce_runtime.profile_requirements_satisfied(
            scope=scope, config_raw=config_raw
        )
    except Exception:
        logger.error(
            "Could not evaluate required conversation extensions for "
            "profile %s at startup; refusing its ingress",
            profile_name,
            exc_info=True,
        )
        readiness[scope] = {
            "ready": False,
            "reason": "requirements_unevaluable",
        }
        return False

    readiness[scope] = {"ready": bool(ok), "reason": reason}
    if not ok:
        logger.error(
            "Profile %s requires a conversation extension that is not "
            "available (%s). Its ingress is refused until the requirement "
            "resolves; other profiles are unaffected.",
            profile_name,
            reason,
        )
        return False

    # Resolve and install this profile's exactly-one ownership plan before
    # it serves traffic. A conflicted or missing required owner makes the
    # profile unready. Ordinary profiles use the inert generic core owner.
    try:
        _plan, _conflicts = self._activate_conversation_ownership(
            profile_name=profile_name, scope=scope, config_raw=config_raw
        )
    except Exception:
        logger.error(
            "Conversation ownership activation raised for profile %s; "
            "refusing its ingress",
            profile_name,
            exc_info=True,
        )
        readiness[scope] = {
            "ready": False,
            "reason": "ownership_unevaluable",
        }
        return False
    if _conflicts:
        readiness[scope] = {
            "ready": False,
            "reason": "ownership_conflict:" + ",".join(_conflicts),
        }
        return False
    return True

def _activate_conversation_extensions_for_served_profiles(self) -> None:
    """Eager startup enumeration of every served profile.

    This is the startup counterpart to the per-message admission gate: it
    runs once, after plugin discovery and before adapters serve, so a
    profile with an unsatisfied hard requirement is known unready *before*
    its first message rather than at first-message time.
    """
    from gateway.run import _multiplex_profile_homes

    try:
        config = getattr(self, "config", None)
        if getattr(config, "multiplex_profiles", False):
            profiles = _multiplex_profile_homes(config)
        else:
            from hermes_cli.profiles import get_active_profile_name, get_profile_dir

            active = get_active_profile_name() or "default"
            profiles = [(active, get_profile_dir(active))]
    except Exception:
        logger.warning(
            "could not enumerate served profiles for conversation "
            "extension activation",
            exc_info=True,
        )
        return

    for profile_name, profile_home in profiles:
        try:
            self._activate_conversation_extensions_for_profile(
                profile_name, Path(profile_home)
            )
        except Exception:
            logger.error(
                "Conversation extension activation failed for profile %s",
                profile_name,
                exc_info=True,
            )

def _extension_profile_is_ready(self, profile_scope: str) -> bool:
    """Return the startup readiness verdict for a canonical home scope.

    Profiles never evaluated are ready, preserving the no-plugin path. The
    key is always ``hermes_home_key(profile_home)``; profile display names
    are not stable identity in single-profile or custom-home deployments.
    """
    readiness = getattr(self, "_extension_profile_readiness", None)
    if not readiness:
        return True
    state = readiness.get(str(profile_scope))
    if not isinstance(state, Mapping):
        return True
    return bool(state.get("ready", True))

def _extension_profile_unready_reason(self, profile_scope: str) -> str:
    readiness = getattr(self, "_extension_profile_readiness", None) or {}
    state = readiness.get(str(profile_scope))
    if isinstance(state, Mapping):
        return str(state.get("reason") or "required_extension_unavailable")
    return "required_extension_unavailable"

# -- exactly-one ownership activation -----------------------------------







def _activate_conversation_ownership(
    self, *, profile_name: str, scope: str, config_raw
):
    """Resolve and install one profile's ownership plan at startup.

    Ownership is decided once, here, before the profile serves traffic. A
    conflicted plan (unowned domain, ambiguous claimant, or a coupled group
    split between owners) is not installed and is reported to the caller,
    which marks the profile unready.
    """
    from gateway.conversation_ownership import activate_plan, describe_plan

    plan, conflicts = activate_plan(scope=scope, config_raw=config_raw)
    if conflicts:
        logger.error(
            "Conversation ownership for profile %s is not activatable (%s); "
            "refusing its ingress.",
            profile_name,
            ", ".join(conflicts),
        )
    else:
        logger.info(
            "Conversation ownership for profile %s: %s",
            profile_name,
            describe_plan(plan),
        )
    return plan, conflicts

def _collect_extension_turn_augmentation(
    self,
    *,
    scope: str,
    session_key: str,
    runtime_profile: str,
    platform: str,
    sender_identity: str,
    chat_type: str,
    user_text: str,
    profile_home: str = "",
    session_id: str = "",
    principal: str = "",
    subject_id: str = "",
    turn_index: int = 0,
    now_timestamp: Optional[float] = None,
    current_message_id: Optional[str] = None,
    conversation_history: tuple = (),
):
    """Return the proven turn-policy owner's complete augmentation.

    Core and unowned verdicts return an empty augmentation. Extension
    errors remain fail-open and are surfaced through ``degraded`` when the
    generic collector can construct a result.
    """
    from gateway.conversation_extensions import GatewayTurnAugmentation

    if not scope:
        return GatewayTurnAugmentation()
    try:
        from gateway import conversation_extension_runtime as _ce_runtime
        from gateway.conversation_ownership import (
            OwnershipDomain,
            conversation_ownership_registry,
        )

        owner = conversation_ownership_registry.owner(
            scope, OwnershipDomain.TURN_POLICY
        )
        if not owner.is_extension or not owner.extension_id:
            return GatewayTurnAugmentation()
        augmentation = _ce_runtime.augment_turn(
            scope=scope,
            session_key=session_key,
            runtime_profile=runtime_profile,
            platform=platform,
            sender_identity=sender_identity,
            chat_type=chat_type,
            user_text=user_text,
            profile_home=profile_home,
            session_id=session_id,
            principal=principal,
            subject_id=subject_id,
            turn_index=turn_index,
            now_timestamp=now_timestamp,
            current_message_id=current_message_id,
            conversation_history=conversation_history,
            extension_id=owner.extension_id,
        )
    except Exception:
        logger.debug("conversation extension turn augmentation failed", exc_info=True)
        return GatewayTurnAugmentation(degraded=True)
    if augmentation.degraded:
        logger.debug(
            "conversation extension turn augmentation degraded for scope %s", scope
        )
    return augmentation

def _collect_extension_turn_context(
    self,
    *,
    scope: str,
    session_key: str,
    runtime_profile: str,
    platform: str,
    sender_identity: str,
    chat_type: str,
    user_text: str,
) -> str:
    """Compatibility wrapper returning only extension user context."""
    augmentation = self._collect_extension_turn_augmentation(
        scope=scope,
        session_key=session_key,
        runtime_profile=runtime_profile,
        platform=platform,
        sender_identity=sender_identity,
        chat_type=chat_type,
        user_text=user_text,
    )
    return "\n\n".join(part for part in augmentation.user_context if part)

async def _run_agent_turn_with_policy(
    self,
    *,
    event,
    source,
    quick_key: str,
    run_generation: int,
    agent_kwargs: dict,
    policy_scope: Optional[str] = None,
):
    """Run one agent turn bound to the extension request policy.

    The policy token is what makes final-dispatch tool authorization
    mandatory rather than advisory: the ContextVar is carried into every
    tool-executor worker thread by
    ``tools.thread_context.propagate_context_to_thread``.

    Extracted from ``_handle_message`` so the ambiguous-authorizer refusal
    is reachable in tests through the real wiring. ``turn_policy_scope``
    resolves the owner eagerly *at call time*, so an ambiguous owner raises
    here — where it is caught and converted into a clean refusal — instead
    of at ``__enter__``, where the previous guard could never fire.

    No token is bound when no extension declares tool authorization for
    this profile, so ordinary turns are completely unaffected.
    """
    from gateway.conversation_extension_runtime import (
        AmbiguousToolAuthorizationOwner as _AmbiguousToolOwner,
        turn_policy_scope as _turn_policy_scope,
    )
    from hermes_constants import hermes_home_key as _hhk

    scope_key = policy_scope
    if not scope_key:
        try:
            scope_key = _hhk(self._resolve_profile_home_for_source(source))
        except Exception:
            logger.debug("could not resolve policy scope", exc_info=True)
            scope_key = ""
    route_id = str(getattr(event, "message_id", None) or quick_key or "turn")

    try:
        policy_ctx = _turn_policy_scope(scope=scope_key, route_id=route_id)
    except _AmbiguousToolOwner:
        # Two extensions claim tool authorization for this profile.
        # Choosing one would let its *allow* bypass the other's *deny*, so
        # the turn is refused rather than run unauthorized.
        logger.error(
            "Refusing turn: multiple conversation extensions claim tool "
            "authorization for this profile"
        )
        return None
    except Exception:
        logger.error(
            "Refusing turn: conversation extension tool-authorization "
            "owner could not be resolved",
            exc_info=True,
        )
        return None

    with policy_ctx:
        return await self._handle_message_with_agent(
            event,
            source,
            quick_key,
            run_generation,
            **agent_kwargs,
        )


def _route_busy_event_through_extension(self, event):
    """Apply extension admission before an adapter's busy-session fast path."""
    source = getattr(event, "source", None)
    if source is None:
        return False
    try:
        from gateway import conversation_extension_runtime as runtime
        from gateway.run import _load_gateway_config_for_profile

        transport_profile = str(getattr(source, "profile", None) or "default")
        transport_home = str(self._resolve_profile_home_for_source(source))
        scope, requirements_profile = self._admission_scope_for_source(
            source, transport_home
        )
        if not self._extension_profile_is_ready(scope):
            return False
        config_raw = _load_gateway_config_for_profile(
            requirements_profile or getattr(source, "profile", None)
        )
        requirements_ok, _ = runtime.profile_requirements_satisfied(
            scope=scope,
            config_raw=config_raw,
        )
        if not requirements_ok:
            return False
        context = runtime.build_route_context(
            event,
            transport_profile=transport_profile,
            transport_home=transport_home,
        )
        if context is None:
            return None
        decision = runtime.admit_and_route(
            context,
            scope=scope,
            served_profiles=tuple(self._served_profile_names()),
            permitted_routes=self._permitted_extension_routes(),
        )
        if decision is None:
            return None
        if not decision.admitted:
            return False
        _, routed_event = self._apply_extension_route_decision_safe(
            source, event, decision
        )
        return routed_event if routed_event is not None else False
    except Exception:
        logger.debug("busy-session extension admission failed", exc_info=True)
        try:
            from gateway import conversation_extension_runtime as fallback_runtime
            from gateway.run import _load_gateway_config_for_profile
            from hermes_constants import hermes_home_key

            scope = hermes_home_key(self._resolve_profile_home_for_source(source))
            ok, _ = fallback_runtime.profile_requirements_satisfied(
                scope=scope,
                config_raw=_load_gateway_config_for_profile(
                    getattr(source, "profile", None)
                ),
            )
            return None if ok else False
        except Exception:
            return False


class ConversationExtensionHostMixin:
    """Collision-isolated GatewayRunner implementation for extension hosting."""

    _install_conversation_extension_host = _install_conversation_extension_host
    _served_profile_names = _served_profile_names
    _admission_scope_for_source = _admission_scope_for_source
    _permitted_extension_routes = _permitted_extension_routes
    _apply_extension_route_decision = _apply_extension_route_decision
    _apply_extension_route_decision_safe = _apply_extension_route_decision_safe
    _extension_runtime_profile = _extension_runtime_profile
    _fire_extension_gateway_start = _fire_extension_gateway_start
    _fire_extension_gateway_stop = _fire_extension_gateway_stop
    _activate_conversation_extensions_for_profile = (
        _activate_conversation_extensions_for_profile
    )
    _activate_conversation_extensions_for_served_profiles = (
        _activate_conversation_extensions_for_served_profiles
    )
    _extension_profile_is_ready = _extension_profile_is_ready
    _extension_profile_unready_reason = _extension_profile_unready_reason
    _activate_conversation_ownership = _activate_conversation_ownership
    _collect_extension_turn_augmentation = _collect_extension_turn_augmentation
    _collect_extension_turn_context = _collect_extension_turn_context
    _run_agent_turn_with_policy = _run_agent_turn_with_policy
    _route_busy_event_through_extension = _route_busy_event_through_extension
