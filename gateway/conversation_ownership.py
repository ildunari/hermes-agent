"""Generic exactly-one conversation-ownership selector (Checkpoint 3).

Checkpoint 2 gave the gateway a *seam*: a plugin can observe routing, tool
dispatch, ingress, and turn completion. This module answers the next question,
which is the one that actually makes an activation safe:

    For this profile and this domain of behavior, **who owns it** — the
    in-core legacy implementation, or exactly one registered extension?

The point is not indirection for its own sake. A gateway that runs both owners
writes ingress twice, compiles conversation texture twice, claims a proactive
slot twice, and can deliver the same message twice. Every duplicate-effect
edge in the de-carry plan reduces to "two owners were live at once", so the
selector's whole job is to make that state unrepresentable and to make the
*undecidable* state a refusal rather than a coin flip.

Three rules follow from that:

* **Default is legacy.** No configuration means the in-core implementation
  owns everything, exactly as before. An ordinary Hermes profile never touches
  this code path in a way it can notice.
* **Exactly one, or nobody.** For a domain configured to ``extension``, one
  registered, API-compatible, capability-complete, healthy extension owns it.
  Zero claimants and two claimants both resolve to :data:`OwnerKind.UNOWNED`.
* **``UNOWNED`` is not a fallback to legacy.** It is a fail-closed verdict. A
  caller that gets it must refuse the work. Falling back to legacy on an
  ambiguous plan is how you end up with the plugin *and* core both live.

Rollback is therefore a pure configuration switch: the legacy implementation is
never deleted at this checkpoint, so flipping ``extension`` back to ``legacy``
and restarting restores the previous owner with no code change.

This module is deliberately provider-neutral. It contains no plugin name, no
contact or relationship concept, and no product policy; a test asserts that.

Configuration shape (per profile, ``config.yaml``)::

    gateway:
      conversation_ownership:
        default: legacy          # or: extension, extension:<id>
        routing: extension       # optional per-domain override
        ingress: extension
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from gateway.conversation_extensions import (
    GatewayExtensionHealth,
    conversation_extension_registry,
)

logger = logging.getLogger(__name__)


CONFIG_SECTION = "conversation_ownership"
DEFAULT_KEY = "default"


class OwnershipDomain(str, Enum):
    """One independently selectable domain of conversation behavior.

    These are the six places the plan identifies as needing a single owner.
    They are named after generic gateway concerns, not after any product.
    """

    ROUTING = "routing"
    INGRESS = "ingress"
    EXTRACTION = "extraction"
    PROACTIVE_CLAIMS = "proactive_claims"
    CHILD_CREATION = "child_creation"
    DELIVERY = "delivery"


#: The extension capability a claimant must declare to own each domain.
DOMAIN_REQUIRED_CAPABILITY: dict[OwnershipDomain, str] = {
    OwnershipDomain.ROUTING: "admission_policy",
    OwnershipDomain.INGRESS: "ingress_observer",
    OwnershipDomain.EXTRACTION: "post_turn_observer",
    OwnershipDomain.PROACTIVE_CLAIMS: "lifecycle",
    OwnershipDomain.CHILD_CREATION: "initiated_turns",
    OwnershipDomain.DELIVERY: "authenticated_dm",
}


#: Domains that share durable resources and must not be split across owners.
#:
#: Ingress and extraction write the same contact-memory tree; proactive claims,
#: child creation, and delivery share the claim/ledger sequence. Splitting
#: either group is the duplicate-writer / duplicate-send shape, so a plan that
#: does it is reported as a conflict rather than started.
COUPLED_DOMAIN_GROUPS: tuple[tuple[OwnershipDomain, ...], ...] = (
    (OwnershipDomain.INGRESS, OwnershipDomain.EXTRACTION),
    (
        OwnershipDomain.PROACTIVE_CLAIMS,
        OwnershipDomain.CHILD_CREATION,
        OwnershipDomain.DELIVERY,
    ),
)


class OwnerKind(str, Enum):
    LEGACY = "legacy"
    EXTENSION = "extension"
    UNOWNED = "unowned"


@dataclass(frozen=True)
class OwnerSelection:
    """The resolved owner of one domain for one profile scope."""

    domain: OwnershipDomain
    kind: OwnerKind
    extension_id: Optional[str] = None
    generation: Optional[int] = None
    reason: str = ""

    @property
    def is_legacy(self) -> bool:
        return self.kind is OwnerKind.LEGACY

    @property
    def is_extension(self) -> bool:
        return self.kind is OwnerKind.EXTENSION

    @property
    def is_unowned(self) -> bool:
        return self.kind is OwnerKind.UNOWNED

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"owner": self.kind.value}
        if self.extension_id:
            payload["extension_id"] = self.extension_id
        if self.generation is not None:
            payload["generation"] = self.generation
        if self.reason:
            payload["reason"] = self.reason
        return payload


# ---------------------------------------------------------------------------
# configuration parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OwnershipRequest:
    """A parsed per-domain request: legacy, or an extension (optionally pinned)."""

    kind: OwnerKind
    pinned_extension_id: Optional[str] = None
    error: str = ""


_LEGACY_REQUEST = _OwnershipRequest(OwnerKind.LEGACY)


def _parse_request(value: Any) -> _OwnershipRequest:
    if value is None:
        return _LEGACY_REQUEST
    if not isinstance(value, str):
        return _OwnershipRequest(OwnerKind.UNOWNED, error="unknown_owner_value")
    token = value.strip().lower()
    if token in ("", "legacy", "core"):
        return _LEGACY_REQUEST
    if token == "extension":
        return _OwnershipRequest(OwnerKind.EXTENSION)
    if token.startswith("extension:"):
        pinned = value.strip()[len("extension:") :].strip()
        if not pinned:
            return _OwnershipRequest(OwnerKind.UNOWNED, error="unknown_owner_value")
        return _OwnershipRequest(OwnerKind.EXTENSION, pinned_extension_id=pinned)
    return _OwnershipRequest(OwnerKind.UNOWNED, error="unknown_owner_value")


def _ownership_section(config_raw: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Read ``gateway.conversation_ownership``; a malformed shape means legacy.

    A shape error here must not take an ordinary gateway out of service: it
    cannot establish that anyone asked for an extension owner, so the safe
    interpretation is the pre-existing behavior. Values that *are* readable
    but unrecognized are a different story and fail closed below.
    """
    try:
        gateway_cfg = (config_raw or {}).get("gateway")
        if not isinstance(gateway_cfg, Mapping):
            return {}
        section = gateway_cfg.get(CONFIG_SECTION)
        if section in (None, "", {}):
            return {}
        if not isinstance(section, Mapping):
            logger.warning(
                "gateway.%s must be a mapping; using the legacy owner", CONFIG_SECTION
            )
            return {}
        return section
    except Exception:
        logger.debug("could not read conversation ownership config", exc_info=True)
        return {}


def _request_for(
    domain: OwnershipDomain, section: Mapping[str, Any]
) -> _OwnershipRequest:
    known = {member.value for member in OwnershipDomain} | {DEFAULT_KEY}
    unknown = {key for key in section if key not in known}
    if unknown:
        # A typo'd domain key silently leaves that domain on legacy while the
        # operator believes they moved it. Refuse the whole profile instead.
        logger.error(
            "gateway.%s names unknown domains: %s", CONFIG_SECTION, sorted(unknown)
        )
        return _OwnershipRequest(OwnerKind.UNOWNED, error="unknown_ownership_domain")
    if domain.value in section:
        return _parse_request(section[domain.value])
    return _parse_request(section.get(DEFAULT_KEY))


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def _is_healthy(bundle) -> bool:
    if bundle.health is None:
        # A bundle with no health probe cannot prove it is fit to own a
        # durable resource. Health is required for ownership.
        return False
    try:
        health = bundle.health()
    except Exception:
        logger.warning(
            "extension %s health probe raised during owner selection",
            bundle.extension_id,
            exc_info=True,
        )
        return False
    return isinstance(health, GatewayExtensionHealth) and bool(health.healthy)


def _claimants(domain: OwnershipDomain, scope: str, pinned: Optional[str]):
    required = DOMAIN_REQUIRED_CAPABILITY[domain]
    for extension_id, generation, bundle in conversation_extension_registry.snapshot(
        scope=scope
    ):
        if pinned and extension_id != pinned:
            continue
        if required not in bundle.capabilities:
            continue
        yield extension_id, generation, bundle


def select_owner(
    domain: OwnershipDomain,
    *,
    scope: str,
    config_raw: Mapping[str, Any] | None,
) -> OwnerSelection:
    """Resolve the single owner of *domain* for the profile at *scope*."""
    section = _ownership_section(config_raw)
    request = _request_for(domain, section)

    if request.kind is OwnerKind.UNOWNED:
        return OwnerSelection(domain, OwnerKind.UNOWNED, reason=request.error)
    if request.kind is OwnerKind.LEGACY:
        return OwnerSelection(domain, OwnerKind.LEGACY)

    claimants = list(_claimants(domain, scope, request.pinned_extension_id))
    if not claimants:
        return OwnerSelection(domain, OwnerKind.UNOWNED, reason="no_owner")
    if len(claimants) > 1:
        logger.error(
            "%d extensions claim the '%s' domain in %s: %s. Refusing to pick one.",
            len(claimants),
            domain.value,
            scope,
            sorted(entry[0] for entry in claimants),
        )
        return OwnerSelection(domain, OwnerKind.UNOWNED, reason="ambiguous_owner")

    extension_id, generation, bundle = claimants[0]
    if not _is_healthy(bundle):
        return OwnerSelection(
            domain,
            OwnerKind.UNOWNED,
            extension_id=extension_id,
            reason="unhealthy_owner",
        )
    return OwnerSelection(
        domain,
        OwnerKind.EXTENSION,
        extension_id=extension_id,
        generation=generation,
    )


def select_all(
    *, scope: str, config_raw: Mapping[str, Any] | None
) -> dict[OwnershipDomain, OwnerSelection]:
    """Resolve every domain at once — the startup activation plan."""
    return {
        domain: select_owner(domain, scope=scope, config_raw=config_raw)
        for domain in OwnershipDomain
    }


def plan_conflicts(plan: Mapping[OwnershipDomain, OwnerSelection]) -> tuple[str, ...]:
    """Return the reasons this plan must not be activated.

    Empty means the plan is coherent: every domain has exactly one owner and
    no group of domains that shares a durable resource is split between the
    legacy implementation and an extension.
    """
    conflicts: list[str] = []
    for domain, selection in sorted(plan.items(), key=lambda item: item[0].value):
        if selection.is_unowned:
            conflicts.append(f"{domain.value}:{selection.reason or 'no_owner'}")

    for group in COUPLED_DOMAIN_GROUPS:
        kinds = {
            plan[domain].kind
            for domain in group
            if domain in plan and not plan[domain].is_unowned
        }
        if len(kinds) > 1:
            conflicts.append(
                "split_owner:" + "+".join(domain.value for domain in group)
            )
    return tuple(conflicts)


def describe_plan(
    plan: Mapping[OwnershipDomain, OwnerSelection],
) -> dict[str, dict[str, Any]]:
    """Bounded, JSON-serializable summary for readiness/status output."""
    return {domain.value: plan[domain].as_dict() for domain in sorted(plan, key=lambda d: d.value)}


# ---------------------------------------------------------------------------
# installed plan (startup decides once; hot paths read a dict)
# ---------------------------------------------------------------------------


class ConversationOwnershipRegistry:
    """Process-wide store of the per-scope ownership plan resolved at startup.

    Ownership is decided **once**, eagerly, when the gateway activates a served
    profile — not re-derived per message. That is deliberate: re-resolving on
    the hot path would let a mid-flight plugin unload move ownership between
    two halves of the same turn, which is the split-owner state this module
    exists to prevent. It also keeps the per-tool-call check a dict lookup.

    A scope with no installed plan reads as legacy for every domain, so any
    process that never ran gateway activation (CLI, tests, a bare import)
    behaves exactly as it did before this checkpoint.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.RLock()
        self._plans: dict[str, dict[OwnershipDomain, OwnerSelection]] = {}

    def install(
        self, scope: str, plan: Mapping[OwnershipDomain, OwnerSelection]
    ) -> None:
        if not isinstance(scope, str) or not scope:
            raise ValueError("scope is required")
        with self._lock:
            self._plans[scope] = dict(plan)

    def plan(self, scope: str) -> dict[OwnershipDomain, OwnerSelection]:
        with self._lock:
            installed = self._plans.get(scope)
        if installed is not None:
            return dict(installed)
        return {
            domain: OwnerSelection(domain, OwnerKind.LEGACY) for domain in OwnershipDomain
        }

    def owner(self, scope: str, domain: OwnershipDomain) -> OwnerSelection:
        with self._lock:
            installed = self._plans.get(scope)
        if installed is None:
            return OwnerSelection(domain, OwnerKind.LEGACY)
        return installed.get(domain, OwnerSelection(domain, OwnerKind.LEGACY))

    def is_extension_owned(self, scope: str, domain: OwnershipDomain) -> bool:
        """True only when an extension is the *proven* owner of *domain*.

        Legacy and unowned both answer False, so a caller written as
        ``if not is_extension_owned(...): run_legacy()`` would still run legacy
        on an unowned domain. Callers that must fail closed use
        :meth:`legacy_is_owner` instead, which is False for unowned.
        """
        return self.owner(scope, domain).is_extension

    def legacy_is_owner(self, scope: str, domain: OwnershipDomain) -> bool:
        """True only when the in-core legacy implementation owns *domain*."""
        return self.owner(scope, domain).is_legacy

    def scopes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._plans))

    def clear(self, scope: str | None = None) -> None:
        with self._lock:
            if scope is None:
                self._plans.clear()
            else:
                self._plans.pop(scope, None)

    def reset_for_tests(self) -> None:
        self.clear()


conversation_ownership_registry = ConversationOwnershipRegistry()


def legacy_owns(scope: str, domain: OwnershipDomain) -> bool:
    """Convenience wrapper: may the legacy in-core implementation run here?"""
    return conversation_ownership_registry.legacy_is_owner(scope, domain)


def extension_owns(scope: str, domain: OwnershipDomain) -> bool:
    """Convenience wrapper: is an extension the proven owner here?"""
    return conversation_ownership_registry.is_extension_owned(scope, domain)


def activate_plan(
    *, scope: str, config_raw: Mapping[str, Any] | None
) -> tuple[dict[OwnershipDomain, OwnerSelection], tuple[str, ...]]:
    """Resolve and install one profile's plan. Returns ``(plan, conflicts)``.

    A conflicted plan is **not** installed: the caller marks the profile
    unready instead, so the gateway never serves traffic with an ambiguous or
    split owner. Leaving the previous (default legacy) reading in place is the
    conservative outcome, but the profile is refused regardless.
    """
    plan = select_all(scope=scope, config_raw=config_raw)
    conflicts = plan_conflicts(plan)
    if conflicts:
        logger.error(
            "Refusing to activate conversation ownership for %s: %s",
            scope,
            ", ".join(conflicts),
        )
        return plan, conflicts
    conversation_ownership_registry.install(scope, plan)
    return plan, ()


__all__ = [
    "COUPLED_DOMAIN_GROUPS",
    "CONFIG_SECTION",
    "DEFAULT_KEY",
    "DOMAIN_REQUIRED_CAPABILITY",
    "ConversationOwnershipRegistry",
    "OwnerKind",
    "OwnerSelection",
    "OwnershipDomain",
    "activate_plan",
    "conversation_ownership_registry",
    "describe_plan",
    "extension_owns",
    "legacy_owns",
    "plan_conflicts",
    "select_all",
    "select_owner",
]
