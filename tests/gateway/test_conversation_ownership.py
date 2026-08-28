"""Checkpoint 3 — generic exactly-one conversation-ownership selector.

Written failing-first. The contract under test:

* **Default is legacy.** A profile with no ownership configuration selects the
  legacy in-core owner for every domain, so ordinary Hermes behavior and the
  current Poke/Guest behavior are byte-identical until an operator opts in.
* **Exactly one owner.** When a domain is configured to the extension owner,
  exactly one registered, API-compatible, capability-complete, healthy
  extension may claim it. Zero and two-or-more both resolve to ``UNOWNED``.
* **Fail closed, never silent fallback.** ``UNOWNED`` is *not* legacy. A caller
  that cannot establish an owner must refuse the work rather than quietly
  running the legacy implementation beside a half-activated plugin — that is
  precisely the duplicate-writer/duplicate-send shape this checkpoint exists to
  prevent.
* **No product policy in core.** The module names no plugin, contact,
  relationship, or product concept.
"""

from __future__ import annotations

import pytest

from gateway import conversation_extensions as ce
from gateway import conversation_ownership as co


@pytest.fixture(autouse=True)
def _clean_registry():
    ce.conversation_extension_registry.reset_for_tests()
    yield
    ce.conversation_extension_registry.reset_for_tests()


def _bundle(
    extension_id: str,
    capabilities: set[str],
    *,
    healthy: bool = True,
    health_raises: bool = False,
    api_version: int | None = None,
) -> ce.GatewayConversationExtension:
    kwargs: dict = {}
    if "admission_policy" in capabilities:
        kwargs["authorize_route"] = lambda ctx: ce.GatewayRouteDirective(admit=True)
    if "tool_authorization" in capabilities:
        kwargs["authorize_tool"] = lambda req: ce.GatewayToolAuthorizationDecision(True)
    if "ingress_observer" in capabilities:
        kwargs["observe_ingress"] = lambda ctx: None
    if "post_turn_observer" in capabilities:
        kwargs["observe_turn_result"] = lambda result: None
    if "lifecycle" in capabilities:
        kwargs["on_start"] = lambda facade: None
    if "turn_policy" in capabilities:
        kwargs["augment_turn"] = lambda ctx: ce.GatewayTurnAugmentation()

    def _health():
        if health_raises:
            raise RuntimeError("probe exploded")
        return ce.GatewayExtensionHealth(healthy=healthy)

    capabilities = set(capabilities) | {"health"}
    kwargs["health"] = _health
    return ce.GatewayConversationExtension(
        extension_id=extension_id,
        api_version=ce.EXTENSION_API_VERSION if api_version is None else api_version,
        capabilities=frozenset(capabilities),
        **kwargs,
    )


def _cfg(**domains) -> dict:
    return {"gateway": {"conversation_ownership": dict(domains)}}


# ---------------------------------------------------------------------------
# Domain table
# ---------------------------------------------------------------------------


def test_every_domain_declares_a_required_capability():
    for domain in co.OwnershipDomain:
        required = co.DOMAIN_REQUIRED_CAPABILITY[domain]
        assert required in ce.ALL_CAPABILITIES


def test_module_names_no_product_policy():
    """Core must stay provider-neutral: no plugin/product identifiers."""
    from pathlib import Path

    source = Path(co.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ("poke", "guest", "bluebubbles", "imessage", "contact_memory"):
        assert forbidden not in source, f"core selector mentions {forbidden!r}"


# ---------------------------------------------------------------------------
# Default = legacy
# ---------------------------------------------------------------------------


def test_no_configuration_selects_legacy_for_every_domain():
    for domain in co.OwnershipDomain:
        selection = co.select_owner(domain, scope="/tmp/home-a", config_raw={})
        assert selection.kind is co.OwnerKind.LEGACY
        assert selection.is_legacy
        assert not selection.is_extension
        assert selection.extension_id is None


def test_explicit_legacy_stays_legacy_even_with_a_registered_extension():
    scope = "/tmp/home-legacy"
    ce.conversation_extension_registry.register(
        _bundle("ext", {"admission_policy"}), scope=scope
    )
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING, scope=scope, config_raw=_cfg(routing="legacy")
    )
    assert selection.kind is co.OwnerKind.LEGACY


def test_unreadable_configuration_falls_back_to_legacy_not_unowned():
    """A malformed *shape* must not take an ordinary gateway out of service."""
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING, scope="/tmp/home-b", config_raw={"gateway": 5}
    )
    assert selection.kind is co.OwnerKind.LEGACY


def test_unknown_owner_value_is_unowned_not_silently_legacy():
    """An operator who typed something we do not understand gets a refusal."""
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING,
        scope="/tmp/home-c",
        config_raw=_cfg(routing="pluginish"),
    )
    assert selection.kind is co.OwnerKind.UNOWNED
    assert selection.reason == "unknown_owner_value"


def test_unknown_domain_key_is_rejected_for_the_whole_profile():
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING,
        scope="/tmp/home-typo",
        config_raw=_cfg(routign="extension"),
    )
    assert selection.kind is co.OwnerKind.UNOWNED
    assert selection.reason == "unknown_ownership_domain"


# ---------------------------------------------------------------------------
# Extension owner
# ---------------------------------------------------------------------------


def test_single_capable_healthy_extension_becomes_the_owner():
    scope = "/tmp/home-owner"
    ce.conversation_extension_registry.register(
        _bundle("alpha", {"admission_policy"}), scope=scope
    )
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING, scope=scope, config_raw=_cfg(routing="extension")
    )
    assert selection.kind is co.OwnerKind.EXTENSION
    assert selection.extension_id == "alpha"
    assert selection.generation is not None


def test_extension_requested_but_none_registered_is_unowned():
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING,
        scope="/tmp/home-empty",
        config_raw=_cfg(routing="extension"),
    )
    assert selection.kind is co.OwnerKind.UNOWNED
    assert selection.reason == "no_owner"
    assert not selection.is_legacy


def test_two_claimants_fail_closed_rather_than_picking_one():
    scope = "/tmp/home-ambiguous"
    ce.conversation_extension_registry.register(
        _bundle("alpha", {"admission_policy"}), scope=scope
    )
    ce.conversation_extension_registry.register(
        _bundle("beta", {"admission_policy"}), scope=scope
    )
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING, scope=scope, config_raw=_cfg(routing="extension")
    )
    assert selection.kind is co.OwnerKind.UNOWNED
    assert selection.reason == "ambiguous_owner"


def test_pinned_owner_id_selects_only_that_extension():
    scope = "/tmp/home-pinned"
    ce.conversation_extension_registry.register(
        _bundle("alpha", {"admission_policy"}), scope=scope
    )
    ce.conversation_extension_registry.register(
        _bundle("beta", {"admission_policy"}), scope=scope
    )
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING,
        scope=scope,
        config_raw=_cfg(routing="extension:beta"),
    )
    assert selection.kind is co.OwnerKind.EXTENSION
    assert selection.extension_id == "beta"


def test_pinned_owner_missing_is_unowned():
    scope = "/tmp/home-pin-missing"
    ce.conversation_extension_registry.register(
        _bundle("alpha", {"admission_policy"}), scope=scope
    )
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING,
        scope=scope,
        config_raw=_cfg(routing="extension:beta"),
    )
    assert selection.kind is co.OwnerKind.UNOWNED
    assert selection.reason == "no_owner"


def test_missing_capability_disqualifies_the_extension():
    scope = "/tmp/home-nocap"
    ce.conversation_extension_registry.register(
        _bundle("alpha", {"tool_authorization"}), scope=scope
    )
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING, scope=scope, config_raw=_cfg(routing="extension")
    )
    assert selection.kind is co.OwnerKind.UNOWNED
    assert selection.reason == "no_owner"


def test_unhealthy_extension_cannot_own():
    scope = "/tmp/home-sick"
    ce.conversation_extension_registry.register(
        _bundle("alpha", {"admission_policy"}, healthy=False), scope=scope
    )
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING, scope=scope, config_raw=_cfg(routing="extension")
    )
    assert selection.kind is co.OwnerKind.UNOWNED
    assert selection.reason == "unhealthy_owner"


def test_raising_health_probe_cannot_own():
    scope = "/tmp/home-raise"
    ce.conversation_extension_registry.register(
        _bundle("alpha", {"admission_policy"}, health_raises=True), scope=scope
    )
    selection = co.select_owner(
        co.OwnershipDomain.ROUTING, scope=scope, config_raw=_cfg(routing="extension")
    )
    assert selection.kind is co.OwnerKind.UNOWNED
    assert selection.reason == "unhealthy_owner"


def test_scopes_are_isolated():
    owner_scope = "/tmp/home-one"
    other_scope = "/tmp/home-two"
    ce.conversation_extension_registry.register(
        _bundle("alpha", {"admission_policy"}), scope=owner_scope
    )
    assert (
        co.select_owner(
            co.OwnershipDomain.ROUTING,
            scope=owner_scope,
            config_raw=_cfg(routing="extension"),
        ).kind
        is co.OwnerKind.EXTENSION
    )
    assert (
        co.select_owner(
            co.OwnershipDomain.ROUTING,
            scope=other_scope,
            config_raw=_cfg(routing="extension"),
        ).kind
        is co.OwnerKind.UNOWNED
    )


def test_default_key_applies_to_every_domain():
    scope = "/tmp/home-default"
    caps = {cap for cap in co.DOMAIN_REQUIRED_CAPABILITY.values()}
    ce.conversation_extension_registry.register(_bundle("alpha", caps), scope=scope)
    plan = co.select_all(scope=scope, config_raw=_cfg(default="extension"))
    assert set(plan) == set(co.OwnershipDomain)
    assert all(sel.kind is co.OwnerKind.EXTENSION for sel in plan.values())


def test_per_domain_override_beats_default():
    scope = "/tmp/home-mixed"
    caps = {cap for cap in co.DOMAIN_REQUIRED_CAPABILITY.values()}
    ce.conversation_extension_registry.register(_bundle("alpha", caps), scope=scope)
    plan = co.select_all(
        scope=scope, config_raw=_cfg(default="extension", routing="legacy")
    )
    assert plan[co.OwnershipDomain.ROUTING].kind is co.OwnerKind.LEGACY
    assert plan[co.OwnershipDomain.INGRESS].kind is co.OwnerKind.EXTENSION


# ---------------------------------------------------------------------------
# Split-owner safety: the anti-duplicate invariant
# ---------------------------------------------------------------------------


def test_partial_extension_activation_is_reported_as_a_conflict():
    """A half-activated plan is exactly the duplicate-writer shape.

    Domains that share a durable resource must not be split across owners.
    """
    scope = "/tmp/home-split"
    caps = {cap for cap in co.DOMAIN_REQUIRED_CAPABILITY.values()}
    ce.conversation_extension_registry.register(_bundle("alpha", caps), scope=scope)
    plan = co.select_all(
        scope=scope,
        config_raw=_cfg(
            default="legacy",
            ingress="extension",
        ),
    )
    conflicts = co.plan_conflicts(plan)
    assert conflicts, "splitting a coupled group must be reported"


def test_a_fully_legacy_plan_has_no_conflicts():
    plan = co.select_all(scope="/tmp/home-clean", config_raw={})
    assert co.plan_conflicts(plan) == ()


def test_a_fully_extension_plan_has_no_conflicts():
    scope = "/tmp/home-full"
    caps = {cap for cap in co.DOMAIN_REQUIRED_CAPABILITY.values()}
    ce.conversation_extension_registry.register(_bundle("alpha", caps), scope=scope)
    plan = co.select_all(scope=scope, config_raw=_cfg(default="extension"))
    assert co.plan_conflicts(plan) == ()


def test_unowned_domain_is_always_a_conflict():
    plan = co.select_all(
        scope="/tmp/home-unowned", config_raw=_cfg(default="extension")
    )
    conflicts = co.plan_conflicts(plan)
    assert conflicts
    assert any("no_owner" in conflict for conflict in conflicts)


def test_plan_summary_is_bounded_and_serializable():
    import json

    plan = co.select_all(scope="/tmp/home-summary", config_raw={})
    payload = co.describe_plan(plan)
    json.dumps(payload)
    assert set(payload) == {domain.value for domain in co.OwnershipDomain}
    assert all(entry["owner"] == "legacy" for entry in payload.values())


# ---------------------------------------------------------------------------
# Rollback: config-only switch
# ---------------------------------------------------------------------------


def test_rollback_is_a_pure_config_switch():
    """Same registry, different config -> the legacy owner is live again."""
    scope = "/tmp/home-rollback"
    caps = {cap for cap in co.DOMAIN_REQUIRED_CAPABILITY.values()}
    ce.conversation_extension_registry.register(_bundle("alpha", caps), scope=scope)

    activated = co.select_all(scope=scope, config_raw=_cfg(default="extension"))
    assert all(sel.is_extension for sel in activated.values())

    rolled_back = co.select_all(scope=scope, config_raw=_cfg(default="legacy"))
    assert all(sel.is_legacy for sel in rolled_back.values())
    assert co.plan_conflicts(rolled_back) == ()


def test_legacy_owner_remains_importable_after_activation():
    """Rollback requires the legacy implementation to still exist in core."""
    import importlib

    for module in (
        "gateway.guest_access",
        "gateway.proactive_scheduler",
        "gateway.proactive_transport",
        "gateway.contact_memory.runtime",
    ):
        assert importlib.import_module(module) is not None


# ---------------------------------------------------------------------------
# Installed plan registry: startup decides once, hot paths read a dict
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_ownership():
    co.conversation_ownership_registry.reset_for_tests()
    yield
    co.conversation_ownership_registry.reset_for_tests()


def test_uninstalled_scope_reads_legacy_for_every_domain():
    """Any process that never ran gateway activation behaves as before."""
    for domain in co.OwnershipDomain:
        assert co.legacy_owns("/tmp/never-activated", domain)
        assert not co.extension_owns("/tmp/never-activated", domain)


def test_activate_plan_installs_a_clean_plan():
    scope = "/tmp/home-install"
    caps = set(co.DOMAIN_REQUIRED_CAPABILITY.values())
    ce.conversation_extension_registry.register(_bundle("alpha", caps), scope=scope)
    plan, conflicts = co.activate_plan(scope=scope, config_raw=_cfg(default="extension"))
    assert conflicts == ()
    for domain in co.OwnershipDomain:
        assert co.extension_owns(scope, domain)
        assert not co.legacy_owns(scope, domain)
    assert plan[co.OwnershipDomain.ROUTING].extension_id == "alpha"


def test_activate_plan_refuses_to_install_a_conflicted_plan():
    scope = "/tmp/home-conflict"
    plan, conflicts = co.activate_plan(
        scope=scope, config_raw=_cfg(default="extension")
    )
    assert conflicts
    assert scope not in co.conversation_ownership_registry.scopes()


def test_unowned_domain_denies_both_owners():
    """`UNOWNED` must not read as legacy — that is the silent-fallback bug."""
    scope = "/tmp/home-unowned-read"
    co.conversation_ownership_registry.install(
        scope,
        {
            co.OwnershipDomain.INGRESS: co.OwnerSelection(
                co.OwnershipDomain.INGRESS, co.OwnerKind.UNOWNED, reason="ambiguous_owner"
            )
        },
    )
    assert not co.legacy_owns(scope, co.OwnershipDomain.INGRESS)
    assert not co.extension_owns(scope, co.OwnershipDomain.INGRESS)


def test_installed_plan_is_not_re_derived_when_the_extension_vanishes():
    """Ownership is frozen at startup so a mid-turn unload cannot split it."""
    scope = "/tmp/home-frozen"
    caps = set(co.DOMAIN_REQUIRED_CAPABILITY.values())
    generation = ce.conversation_extension_registry.register(
        _bundle("alpha", caps), scope=scope
    )
    co.activate_plan(scope=scope, config_raw=_cfg(default="extension"))
    ce.conversation_extension_registry.unregister(
        "alpha", generation=generation, scope=scope
    )
    # Still extension-owned: the legacy path must not silently resume mid-run.
    assert co.extension_owns(scope, co.OwnershipDomain.INGRESS)
    assert not co.legacy_owns(scope, co.OwnershipDomain.INGRESS)


def test_installed_plans_are_scope_isolated():
    caps = set(co.DOMAIN_REQUIRED_CAPABILITY.values())
    activated = "/tmp/home-act"
    ordinary = "/tmp/home-ordinary"
    ce.conversation_extension_registry.register(_bundle("alpha", caps), scope=activated)
    co.activate_plan(scope=activated, config_raw=_cfg(default="extension"))
    assert co.extension_owns(activated, co.OwnershipDomain.DELIVERY)
    assert co.legacy_owns(ordinary, co.OwnershipDomain.DELIVERY)


def test_rollback_reinstalls_legacy_over_an_activated_scope():
    scope = "/tmp/home-rollback-install"
    caps = set(co.DOMAIN_REQUIRED_CAPABILITY.values())
    ce.conversation_extension_registry.register(_bundle("alpha", caps), scope=scope)
    co.activate_plan(scope=scope, config_raw=_cfg(default="extension"))
    assert co.extension_owns(scope, co.OwnershipDomain.DELIVERY)

    # Config-only switch + restart == re-running activation with legacy config.
    _plan, conflicts = co.activate_plan(scope=scope, config_raw=_cfg(default="legacy"))
    assert conflicts == ()
    for domain in co.OwnershipDomain:
        assert co.legacy_owns(scope, domain)
        assert not co.extension_owns(scope, domain)


def test_reactivation_is_stable_across_restart():
    """Same registry + same config -> byte-identical plan, twice."""
    scope = "/tmp/home-restart"
    caps = set(co.DOMAIN_REQUIRED_CAPABILITY.values())
    ce.conversation_extension_registry.register(_bundle("alpha", caps), scope=scope)
    first, _ = co.activate_plan(scope=scope, config_raw=_cfg(default="extension"))
    co.conversation_ownership_registry.reset_for_tests()
    second, _ = co.activate_plan(scope=scope, config_raw=_cfg(default="extension"))
    assert co.describe_plan(first) == co.describe_plan(second)
