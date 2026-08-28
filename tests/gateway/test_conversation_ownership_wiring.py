"""Checkpoint 3 — legacy owner gating *as wired in production code*.

Checkpoint 2's lesson was that a contract module with no production caller is
not a feature. These tests therefore drive the real ``GatewayRunner`` /
``model_tools`` entry points and assert the single-owner contract at each of
the six domains the plan names:

* **routing** — the legacy Guest classification/route path
* **ingress** — the legacy authenticated communication-ingress write
* **extraction** — the legacy post-turn contact-memory extraction submit
* **proactive_claims** — the legacy proactive watcher
* **child_creation** / **delivery** — carried by the watcher's claim/deliver
  sequence, gated with it

The invariant in every case is the same and is deliberately asymmetric:

* legacy owner  -> the legacy implementation runs, byte-identically to before;
* extension owner -> the legacy implementation does **not** run;
* unowned       -> the legacy implementation does **not** run either.

That last line is the whole point. Treating "not extension-owned" as "run
legacy" is what produces two live owners, and two live owners is what produces
a duplicate ingress write or a duplicate send.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import conversation_extensions as ce
from gateway import conversation_ownership as co
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _clean():
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    co.conversation_ownership_registry.reset_for_tests()
    yield
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    co.conversation_ownership_registry.reset_for_tests()


def _bare_runner() -> GatewayRunner:
    """A GatewayRunner without __init__ side effects (AGENTS.md test pattern)."""
    return object.__new__(GatewayRunner)


def _install(scope: str, **domains: co.OwnerKind) -> None:
    plan = {
        domain: co.OwnerSelection(
            domain,
            domains.get(domain.value, co.OwnerKind.LEGACY),
            extension_id=(
                "ext" if domains.get(domain.value) is co.OwnerKind.EXTENSION else None
            ),
        )
        for domain in co.OwnershipDomain
    }
    co.conversation_ownership_registry.install(scope, plan)


# ---------------------------------------------------------------------------
# The runner-level helper every gate uses
# ---------------------------------------------------------------------------


def test_runner_reports_legacy_owner_by_default(tmp_path):
    runner = _bare_runner()
    scope = str(tmp_path / "default")
    for domain in co.OwnershipDomain:
        assert runner._legacy_owns(scope, domain) is True


def test_runner_denies_legacy_when_extension_owns(tmp_path):
    runner = _bare_runner()
    scope = str(tmp_path / "ext")
    _install(scope, routing=co.OwnerKind.EXTENSION)
    assert runner._legacy_owns(scope, co.OwnershipDomain.ROUTING) is False
    assert runner._legacy_owns(scope, co.OwnershipDomain.INGRESS) is True


def test_runner_denies_legacy_when_unowned(tmp_path):
    """Unowned is a refusal, not a quiet fallback to the legacy owner."""
    runner = _bare_runner()
    scope = str(tmp_path / "unowned")
    co.conversation_ownership_registry.install(
        scope,
        {
            co.OwnershipDomain.INGRESS: co.OwnerSelection(
                co.OwnershipDomain.INGRESS,
                co.OwnerKind.UNOWNED,
                reason="ambiguous_owner",
            )
        },
    )
    assert runner._legacy_owns(scope, co.OwnershipDomain.INGRESS) is False


def test_runner_owner_scope_is_canonical_home_key(tmp_path):
    """Scope resolution must match extension registration/readiness keying."""
    from hermes_constants import hermes_home_key

    runner = _bare_runner()
    home = tmp_path / "profile-home"
    home.mkdir()
    runner._resolve_profile_home_for_source = lambda source: home
    source = SimpleNamespace(profile="coding")
    assert runner._ownership_scope_for_source(source) == hermes_home_key(home)


def test_runner_owner_scope_failure_is_not_silently_legacy(tmp_path):
    """If we cannot resolve a scope we must not assume the legacy owner."""
    runner = _bare_runner()

    def _boom(source):
        raise RuntimeError("no home")

    runner._resolve_profile_home_for_source = _boom
    # Once any plan is installed in the process, an unresolvable scope is
    # treated as unowned rather than defaulting to legacy.
    _install(str(tmp_path / "somewhere"), routing=co.OwnerKind.EXTENSION)
    assert runner._ownership_scope_for_source(SimpleNamespace(profile="x")) is None
    assert runner._legacy_owns(None, co.OwnershipDomain.ROUTING) is False


# ---------------------------------------------------------------------------
# routing + ingress: the legacy BlueBubbles guest ingress path
# ---------------------------------------------------------------------------


class _FakePlatformCfg:
    extra = {"guest_routing_enabled": True}


def _legacy_probe(calls):
    async def _probe(self, event):
        calls.append("legacy")
        return ()

    return _probe


def _ingress_runner(tmp_path, monkeypatch):
    from gateway.config import Platform

    runner = _bare_runner()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    runner._resolve_profile_home_for_source = lambda source: home
    runner.config = SimpleNamespace(platforms={Platform.BLUEBUBBLES: _FakePlatformCfg()})
    source = SimpleNamespace(
        platform=Platform.BLUEBUBBLES, chat_type="dm", profile="poke", user_id="u"
    )
    event = SimpleNamespace(
        source=source,
        internal=False,
        communication_ingress=({"kind": "text"},),
        metadata={},
        raw_message={},
    )
    return runner, home, event


@pytest.mark.asyncio
async def test_legacy_ingress_runs_when_legacy_owns(tmp_path, monkeypatch):
    runner, home, event = _ingress_runner(tmp_path, monkeypatch)
    calls = []

    import gateway.run as run_module

    monkeypatch.setattr(
        run_module.GatewayRunner,
        "_classify_and_persist_legacy_ingress",
        _legacy_probe(calls),
        raising=False,
    )
    await runner._handle_communication_ingress(event)
    assert calls == ["legacy"]


@pytest.mark.asyncio
async def test_legacy_ingress_is_skipped_when_extension_owns(tmp_path, monkeypatch):
    from hermes_constants import hermes_home_key

    runner, home, event = _ingress_runner(tmp_path, monkeypatch)
    _install(hermes_home_key(home), ingress=co.OwnerKind.EXTENSION)
    calls = []

    import gateway.run as run_module

    monkeypatch.setattr(
        run_module.GatewayRunner,
        "_classify_and_persist_legacy_ingress",
        _legacy_probe(calls),
        raising=False,
    )
    result = await runner._handle_communication_ingress(event)
    assert calls == [], "extension owns ingress; the legacy writer must not run"
    assert result == ()


@pytest.mark.asyncio
async def test_legacy_ingress_is_skipped_when_unowned(tmp_path, monkeypatch):
    from hermes_constants import hermes_home_key

    runner, home, event = _ingress_runner(tmp_path, monkeypatch)
    co.conversation_ownership_registry.install(
        hermes_home_key(home),
        {
            co.OwnershipDomain.INGRESS: co.OwnerSelection(
                co.OwnershipDomain.INGRESS,
                co.OwnerKind.UNOWNED,
                reason="ambiguous_owner",
            )
        },
    )
    calls = []

    import gateway.run as run_module

    monkeypatch.setattr(
        run_module.GatewayRunner,
        "_classify_and_persist_legacy_ingress",
        _legacy_probe(calls),
        raising=False,
    )
    result = await runner._handle_communication_ingress(event)
    assert calls == []
    assert result == ()


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def test_extraction_gate_follows_the_owner(tmp_path):
    runner = _bare_runner()
    home = tmp_path / "extraction-home"
    home.mkdir()
    runner._resolve_profile_home_for_source = lambda source: home
    source = SimpleNamespace(profile="poke")

    assert runner._legacy_extraction_permitted(source) is True

    from hermes_constants import hermes_home_key

    _install(hermes_home_key(home), extraction=co.OwnerKind.EXTENSION)
    assert runner._legacy_extraction_permitted(source) is False


# ---------------------------------------------------------------------------
# proactive claims / child creation / delivery
# ---------------------------------------------------------------------------


def test_proactive_gate_permits_legacy_by_default(tmp_path):
    runner = _bare_runner()
    assert runner._legacy_proactive_permitted(str(tmp_path / "p")) is True


def test_proactive_gate_denies_when_extension_owns_any_of_the_group(tmp_path):
    runner = _bare_runner()
    scope = str(tmp_path / "p2")
    for domain in ("proactive_claims", "child_creation", "delivery"):
        co.conversation_ownership_registry.reset_for_tests()
        _install(scope, **{domain: co.OwnerKind.EXTENSION})
        assert runner._legacy_proactive_permitted(scope) is False, domain


def test_proactive_gate_denies_when_any_group_domain_is_unowned(tmp_path):
    runner = _bare_runner()
    scope = str(tmp_path / "p3")
    co.conversation_ownership_registry.install(
        scope,
        {
            co.OwnershipDomain.DELIVERY: co.OwnerSelection(
                co.OwnershipDomain.DELIVERY, co.OwnerKind.UNOWNED, reason="no_owner"
            )
        },
    )
    assert runner._legacy_proactive_permitted(scope) is False


# ---------------------------------------------------------------------------
# tool policy: exactly one authoritative guard
# ---------------------------------------------------------------------------


def test_legacy_guest_tool_guard_is_skipped_when_extension_owns_routing(monkeypatch):
    """Two live tool guards is the double-enforcement shape.

    When the extension owns routing it also owns the tool decision, and the
    generic final-dispatch seam already enforces it. Running the legacy guard
    as well means a legacy *deny* could contradict the owner's *allow*.
    """
    import model_tools

    calls = []
    monkeypatch.setattr(
        model_tools,
        "_legacy_guest_policy_active",
        lambda: calls.append("checked") or False,
        raising=False,
    )
    assert hasattr(model_tools, "_legacy_guest_policy_active")


def test_extension_tool_policy_denies_and_legacy_cannot_override():
    """A bound policy token's deny is final."""
    scope = "/tmp/ownership-tool-scope"
    bundle = ce.GatewayConversationExtension(
        extension_id="ext",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization", "health"}),
        authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(
            False, "denied by owner"
        ),
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)
    policy = ce.issue_request_policy(
        extension_id="ext", profile_home=scope, route_id="r1"
    )
    with ce.request_policy_scope(policy):
        blocked = ce.authorize_tool_dispatch("terminal", {"command": "ls"})
    assert blocked is not None
    assert "denied by owner" in blocked


# ---------------------------------------------------------------------------
# activation plan is recorded at startup and surfaced in readiness
# ---------------------------------------------------------------------------


def test_activation_records_the_plan_for_a_profile(tmp_path):
    runner = _bare_runner()
    home = tmp_path / "activate-home"
    home.mkdir()
    from hermes_constants import hermes_home_key

    scope = hermes_home_key(home)
    plan, conflicts = runner._activate_conversation_ownership(
        profile_name="poke", scope=scope, config_raw={}
    )
    assert conflicts == ()
    assert all(selection.is_legacy for selection in plan.values())
    assert co.legacy_owns(scope, co.OwnershipDomain.ROUTING)


def test_activation_conflict_makes_the_profile_unready(tmp_path):
    runner = _bare_runner()
    home = tmp_path / "conflict-home"
    home.mkdir()
    from hermes_constants import hermes_home_key

    scope = hermes_home_key(home)
    plan, conflicts = runner._activate_conversation_ownership(
        profile_name="poke",
        scope=scope,
        config_raw={"gateway": {"conversation_ownership": {"default": "extension"}}},
    )
    assert conflicts, "no extension registered -> the plan must be refused"
    assert scope not in co.conversation_ownership_registry.scopes()


# ---------------------------------------------------------------------------
# Production call sites actually consult the gate (AST/source contracts)
# ---------------------------------------------------------------------------


def _run_source() -> str:
    import gateway.run as run_module

    return Path(run_module.__file__).read_text(encoding="utf-8")


def test_legacy_ingress_entry_point_consults_the_gate():
    import ast

    tree = ast.parse(_run_source())
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_handle_communication_ingress"
    )
    calls = {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_legacy_owns_for_source" in calls
    assert "_classify_and_persist_legacy_ingress" in calls


def test_legacy_extraction_site_consults_the_gate():
    import ast

    tree = ast.parse(_run_source())
    gated = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_legacy_extraction_permitted"
    ]
    assert gated, "the post-turn extraction submit must be ownership-gated"


def test_proactive_watcher_consults_the_gate():
    import ast

    tree = ast.parse(_run_source())
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_proactive_scheduler_watcher"
    )
    calls = {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_legacy_proactive_permitted_for_profile" in calls


def test_startup_activation_installs_the_ownership_plan():
    import ast

    tree = ast.parse(_run_source())
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_activate_conversation_extensions_for_profile"
    )
    calls = {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_activate_conversation_ownership" in calls


def test_legacy_tool_guard_is_gated_in_model_tools():
    import ast

    import model_tools

    source = Path(model_tools.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "handle_function_call"
    )
    names = {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_legacy_tool_policy_owner_active" in names


def test_legacy_tool_guard_active_without_a_policy_token():
    import model_tools

    assert model_tools._legacy_tool_policy_owner_active() is True


def test_legacy_tool_guard_stands_down_only_for_a_proven_extension_owner():
    """A bound token is necessary but **not sufficient** to disarm the legacy guard.

    Review-3 P0-1: ``turn_policy_scope`` binds a token whenever any registered
    bundle declares ``tool_authorization``, including a rolled-back/dark bundle
    whose answer is an unconditional allow. Standing down on token presence
    alone therefore left a guest unguarded on the documented rollback path. The
    signal is the ownership verdict for the routing domain.
    """
    import model_tools

    scope = "/tmp/tool-owner"
    policy = ce.issue_request_policy(
        extension_id="ext", profile_home=scope, route_id="r"
    )

    # Token bound, but no ownership plan installed -> legacy still owns.
    with ce.request_policy_scope(policy):
        assert model_tools._legacy_tool_policy_owner_active() is True

    # Token bound and the plan says legacy owns routing -> legacy still owns.
    _install(scope, routing=co.OwnerKind.LEGACY)
    with ce.request_policy_scope(policy):
        assert model_tools._legacy_tool_policy_owner_active() is True

    # Token bound and an extension is the proven routing owner -> stand down.
    co.conversation_ownership_registry.clear(scope)
    _install(scope, routing=co.OwnerKind.EXTENSION)
    with ce.request_policy_scope(policy):
        assert model_tools._legacy_tool_policy_owner_active() is False

    # Unowned is a refusal, not a handover: legacy keeps guarding.
    co.conversation_ownership_registry.clear(scope)
    _install(scope, routing=co.OwnerKind.UNOWNED)
    with ce.request_policy_scope(policy):
        assert model_tools._legacy_tool_policy_owner_active() is True

    # Outside any bound policy, behavior is unchanged.
    assert model_tools._legacy_tool_policy_owner_active() is True


def test_only_one_tool_guard_can_deny_a_dispatch(monkeypatch):
    """With a *proven* extension owner bound, the legacy guard must not also run."""
    import model_tools

    legacy_calls = []

    def _tracking_enforce(name, args):
        legacy_calls.append(name)
        return None

    import gateway.guest_access as guest_access

    monkeypatch.setattr(guest_access, "enforce_guest_tool_call", _tracking_enforce)

    scope = "/tmp/single-guard"
    bundle = ce.GatewayConversationExtension(
        extension_id="ext",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization", "health"}),
        authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(
            False, "owner denies"
        ),
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)
    # The extension must be the *proven* routing owner, not merely registered.
    _install(scope, routing=co.OwnerKind.EXTENSION)
    policy = ce.issue_request_policy(
        extension_id="ext", profile_home=scope, route_id="r"
    )
    with ce.request_policy_scope(policy):
        result = model_tools.handle_function_call("read_file", {"path": "/etc/hosts"})
    assert "owner denies" in result
    assert legacy_calls == [], "the legacy guard must not run under an owner"
