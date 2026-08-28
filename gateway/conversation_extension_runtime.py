"""Production integration of the generic conversation-extension seam.

``gateway/conversation_extensions.py`` defines the contracts; this module is
what the live gateway actually calls. It exists as its own module so the
sequence is testable end-to-end without standing up a whole ``GatewayRunner``,
and so ``gateway/run.py`` gains a handful of narrow calls rather than another
few hundred lines of policy.

The sequence is fixed and must not be reordered:

1. **Transport identity is captured first and is immutable.** The adapter has
   already authenticated the sender under some profile/home; that pair is the
   trust domain and never changes for the life of the request.
2. **Admission/route.** Any registered admission extension returns a typed
   proposal. Core validates it against served profiles and the permitted route
   map. Denials and errors drop the message.
3. **Runtime profile scope.** Only after validation does the request enter the
   routed profile's runtime scope.
4. **Request policy is issued and bound** around the whole turn, including
   every executor/thread hop, so final-dispatch tool authorization is
   mandatory rather than advisory.
5. **Turn augmentation** contributes optional context (fails open).
6. **Post-turn observation** reports the authenticated completion.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Mapping, Optional, Sequence

from gateway.conversation_extensions import (
    GatewayRequestPolicy,
    GatewayRouteContext,
    GatewayRouteDecision,
    GatewayRuntimeFacade,
    GatewayTurnAugmentation,
    GatewayTurnContext,
    GatewayTurnResult,
    collect_turn_augmentation,
    conversation_extension_registry,
    evaluate_extension_readiness,
    gateway_host_operations,
    issue_request_policy,
    lifecycle_task_registry,
    notify_gateway_start,
    notify_gateway_stop,
    notify_ingress,
    notify_turn_result,
    parse_required_extensions,
    request_policy_scope,
    resolve_route,
)

logger = logging.getLogger(__name__)


def _text_preview(value: Any, limit: int = 120) -> str:
    text = str(value or "")
    return text[:limit]


def build_route_context(
    event: Any,
    *,
    transport_profile: str,
    transport_home: str,
    adapter_identity: str = "",
) -> Optional[GatewayRouteContext]:
    """Capture the trusted, pre-authorization description of one message.

    Returns ``None`` when the event does not carry enough identity to build a
    context; callers treat that as "no extension routing", not as an admit.
    """
    source = getattr(event, "source", None)
    if source is None:
        return None
    platform = getattr(source, "platform", None)
    platform_name = getattr(platform, "value", None) or str(platform or "")
    if not platform_name:
        return None
    try:
        return GatewayRouteContext(
            platform=platform_name,
            adapter_identity=adapter_identity or platform_name,
            transport_profile=transport_profile,
            transport_home=transport_home,
            sender_identity=str(getattr(source, "user_id", "") or ""),
            chat_id=str(getattr(source, "chat_id", "") or ""),
            chat_type=str(getattr(source, "chat_type", "") or ""),
            text_preview=_text_preview(getattr(event, "text", "")),
            is_group=str(getattr(source, "chat_type", "")) == "group",
            metadata={},
        )
    except Exception:
        logger.debug("could not build extension route context", exc_info=True)
        return None


def admit_and_route(
    context: Optional[GatewayRouteContext],
    *,
    scope: str,
    served_profiles: Sequence[str] = (),
    permitted_routes: Mapping[str, Sequence[str]] | None = None,
) -> Optional[GatewayRouteDecision]:
    """Run the admission/route step. ``None`` means "no context to route"."""
    if context is None:
        return None
    return resolve_route(
        context,
        scope=scope,
        served_profiles=served_profiles,
        permitted_routes=permitted_routes,
    )


def observe_authenticated_ingress(
    context: Optional[GatewayRouteContext], *, scope: str
) -> None:
    """Fire the authenticated-ingress observation site."""
    if context is None:
        return
    notify_ingress(context, scope=scope)


@contextmanager
def _bound_policy_scope(policy: Optional[GatewayRequestPolicy]):
    """Inner context manager over an already-resolved policy."""
    if policy is None:
        yield None
        return
    with request_policy_scope(policy):
        yield policy


def turn_policy_scope(
    *,
    scope: str,
    route_id: str,
    extension_id: Optional[str] = None,
):
    """Resolve the turn's tool-authorization owner and return a bound scope.

    The token is what makes final-dispatch tool authorization mandatory. It is
    bound with a ContextVar, so every executor/thread hop that uses
    ``tools.thread_context.propagate_context_to_thread`` (the concurrent and
    sequential tool executors, and the code-execution RPC threads) carries it
    automatically.

    When no extension with tool authorization is registered for *scope*, no
    token is bound and behavior is exactly as before.

    **This is deliberately a plain function, not a ``@contextmanager``.** The
    owner is resolved *eagerly, at call time*, so
    :class:`AmbiguousToolAuthorizationOwner` propagates from the call itself
    rather than from ``__enter__``. A generator-based context manager defers
    its whole body until ``with``, which made every caller-side
    ``try/except AmbiguousToolAuthorizationOwner`` around the call dead code
    and turned an ambiguous authorizer into an unhandled per-message crash
    instead of the intended clean refusal.

    Raises :class:`AmbiguousToolAuthorizationOwner` when more than one
    extension claims tool authorization for the profile. Picking one would
    silently bypass the other authorizer, so this is fail-closed by design;
    the caller drops the turn rather than running it unauthorized.
    """
    resolved = extension_id or _tool_authorization_owner(scope)
    if not resolved:
        return _bound_policy_scope(None)
    policy = issue_request_policy(
        extension_id=resolved, profile_home=scope, route_id=route_id
    )
    return _bound_policy_scope(policy)


class AmbiguousToolAuthorizationOwner(RuntimeError):
    """More than one extension claims tool authorization for one profile.

    Choosing an owner arbitrarily would let one extension's *allow* bypass
    another extension's *deny*, so this condition denies the turn instead.
    """


def _tool_authorization_owner(scope: str) -> Optional[str]:
    """Return the single extension owning tool authorization in *scope*.

    Exactly zero owners means "no policy" (ordinary behavior). Exactly one
    owner binds that policy. More than one is unresolvable and fails closed.
    """
    owners = [
        extension_id
        for extension_id, _generation, bundle in conversation_extension_registry.snapshot(
            scope=scope
        )
        if bundle.authorize_tool is not None
    ]
    if not owners:
        return None
    if len(owners) > 1:
        logger.error(
            "Multiple conversation extensions declare tool authorization in %s: %s. "
            "Refusing the turn rather than bypassing one authorizer.",
            scope,
            owners,
        )
        raise AmbiguousToolAuthorizationOwner(
            f"{len(owners)} extensions claim tool authorization in {scope}"
        )
    return owners[0]


def build_profile_facades(
    *, scope: str, profile_name: str
) -> tuple[GatewayRuntimeFacade, ...]:
    """Build one facade per registered extension in *scope*.

    Used by the gateway's eager per-profile activation and by the gateway-stop
    teardown, so the lifecycle callbacks see the same bounded facade the
    registration path hands out (identity + capabilities only; never the
    runner, a store, a client, or credentials).
    """
    host = gateway_host_operations()
    facades: list[GatewayRuntimeFacade] = []
    for extension_id, generation, bundle in conversation_extension_registry.snapshot(
        scope=scope
    ):
        facades.append(
            GatewayRuntimeFacade(
                extension_id=extension_id,
                profile_name=profile_name,
                profile_home=scope,
                generation=generation,
                capabilities=bundle.capabilities,
                host=host,
            )
        )
    return tuple(facades)


def fire_gateway_start(*, scope: str, profile_name: str) -> tuple[str, ...]:
    """Gateway-start lifecycle fire site. Returns the extension ids started."""
    facades = build_profile_facades(scope=scope, profile_name=profile_name)
    if not facades:
        return ()
    notify_gateway_start(facades, scope=scope)
    return tuple(facade.extension_id for facade in facades)


def fire_gateway_stop(*, scope: str, profile_name: str) -> tuple[str, ...]:
    """Gateway-stop lifecycle fire site plus generation-scoped task teardown.

    Without this, a gateway shutdown left every extension's lifecycle tasks
    running through the extension's own contract: ``on_stop`` only fired on
    plugin *unload*, which a process shutdown does not perform.
    """
    facades = build_profile_facades(scope=scope, profile_name=profile_name)
    if not facades:
        return ()
    notify_gateway_stop(facades, scope=scope)
    for facade in facades:
        try:
            lifecycle_task_registry.cancel(
                facade.extension_id, scope, facade.generation
            )
        except Exception:
            logger.debug(
                "failed to cancel lifecycle tasks for %s at gateway stop",
                facade.extension_id,
                exc_info=True,
            )
    return tuple(facade.extension_id for facade in facades)


def augment_turn(
    *,
    scope: str,
    session_key: str,
    runtime_profile: str,
    platform: str,
    sender_identity: str,
    chat_type: str,
    user_text: str,
) -> GatewayTurnAugmentation:
    """Collect optional per-turn context. Never raises into the turn."""
    context = GatewayTurnContext(
        session_key=session_key,
        runtime_profile=runtime_profile,
        platform=platform,
        sender_identity=sender_identity,
        chat_type=chat_type,
        user_text=user_text,
    )
    return collect_turn_augmentation(context, scope=scope)


def observe_turn_completion(
    *,
    scope: str,
    session_key: str,
    runtime_profile: str,
    platform: str,
    sender_identity: str,
    user_text: str,
    assistant_text: str,
    delivered: bool,
    user_message_id: Optional[str] = None,
    assistant_message_id: Optional[str] = None,
) -> None:
    """Fire the post-turn completion site with trusted route identity."""
    try:
        result = GatewayTurnResult(
            session_key=session_key,
            runtime_profile=runtime_profile,
            platform=platform,
            sender_identity=sender_identity,
            user_text=user_text,
            assistant_text=assistant_text,
            delivered=delivered,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
        )
    except Exception:
        logger.debug("could not build extension turn result", exc_info=True)
        return
    notify_turn_result(result, scope=scope)


def profile_requirements_satisfied(
    *,
    scope: str,
    config_raw: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Fail-closed check that a profile may serve traffic.

    This is the *admission* counterpart to the readiness probe. A profile that
    declares ``gateway.required_conversation_extensions`` may not serve a
    single message until every requirement resolves to a registered,
    API-compatible, capability-complete, healthy extension in its own scope.

    Returns ``(True, "")`` for profiles with no requirements, so ordinary
    Hermes profiles are completely unaffected.
    """
    try:
        gateway_cfg = (config_raw or {}).get("gateway")
        raw = (
            gateway_cfg.get("required_conversation_extensions")
            if isinstance(gateway_cfg, Mapping)
            else None
        )
    except Exception:
        return False, "requirements_unreadable"

    if raw in (None, "", (), []):
        return True, ""

    try:
        required = parse_required_extensions(raw)
    except Exception:
        # A profile that *tried* to require an extension must never silently
        # serve without one.
        return False, "malformed_requirement_declaration"

    if not required:
        return True, ""

    report = evaluate_extension_readiness(required=required, scope=scope)
    if report.ready:
        return True, ""
    reasons = ",".join(
        f"{check.extension_id}:{check.reason}" for check in report.checks if not check.ok
    )
    return False, reasons or "required_extension_unavailable"


__all__ = [
    "admit_and_route",
    "augment_turn",
    "build_profile_facades",
    "build_route_context",
    "fire_gateway_start",
    "fire_gateway_stop",
    "observe_authenticated_ingress",
    "observe_turn_completion",
    "profile_requirements_satisfied",
    "turn_policy_scope",
]
