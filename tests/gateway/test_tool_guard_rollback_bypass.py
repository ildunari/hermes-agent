"""Review-3 P0-1 regression: rollback must never disarm the Guest tool guard.

The Checkpoint 3 rollback story is "flip ``conversation_ownership`` back to
``legacy`` and safely restart". The plugin is still *loaded* in that state — it
just registers its dark/observe bundle. Review 3 found that this combination
silently disabled tool policy entirely:

* the legacy guard in ``model_tools.handle_function_call`` stood down whenever a
  request-policy token was bound;
* ``turn_policy_scope`` bound a token whenever *any* registered bundle declared
  ``tool_authorization`` — including the dark bundle;
* the dark bundle's ``authorize_tool`` unconditionally returns *allow*.

Net effect: a guest got unsandboxed ``terminal`` / ``execute_code`` on the exact
path the plan calls "rollback". These tests drive the real
``model_tools.handle_function_call`` dispatcher with the real dark bundle and
assert the malicious call is denied through every dispatch path the plan
enumerates: direct, deferred (tool-search bridge), MCP, and inline/recursive.

The two independent fixes asserted here are:

1. the dark bundle no longer declares ``tool_authorization`` at all, so it
   cannot bind a policy token or answer an authorization question; and
2. the legacy stand-down is gated on the *ownership verdict* for the routing
   domain, not on token presence, so a bound token from some other extension
   still cannot disarm the legacy guard while legacy owns routing.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

from gateway import conversation_extensions as ce
from gateway import conversation_ownership as co


PLUGIN_ROOT = Path(
    os.environ.get(
        "HERMES_POKE_PLUGIN_ROOT",
        "/Users/Kosta/LocalDev/.studio-only/hermes-kosta-plugin-worktrees/poke-plugin-decarry",
    )
)

# The malicious calls the Guest sandbox exists to stop. Both are read straight
# out of the review's executed repro.
MALICIOUS_TERMINAL = (
    "terminal",
    {"command": "cat /Users/Kosta/.hermes/.env", "workdir": "/"},
)
MALICIOUS_EXECUTE_CODE = (
    "execute_code",
    {"code": "import pathlib;print(pathlib.Path('/Users/Kosta/.hermes/.env').read_text())"},
)


def _load_plugin_module(name: str):
    if not PLUGIN_ROOT.is_dir():
        pytest.skip(f"poke plugin worktree not available at {PLUGIN_ROOT}")
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        return importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot import {name} from plugin worktree: {exc}")


@pytest.fixture(scope="module")
def poke_extension():
    return _load_plugin_module("poke.extension")


@pytest.fixture(autouse=True)
def _clean():
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    co.conversation_ownership_registry.reset_for_tests()
    yield
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    co.conversation_ownership_registry.reset_for_tests()


@pytest.fixture
def guest_policy(monkeypatch, tmp_path):
    """Arm the real legacy Guest policy against a temp sandbox root."""
    sandbox = tmp_path / "guest-workspace"
    sandbox.mkdir()
    monkeypatch.setenv("HERMES_GUEST_POLICY", "1")
    monkeypatch.setenv("HERMES_GUEST_SANDBOX_ROOT", str(sandbox))
    import gateway.guest_access as guest_access

    with guest_access.guest_policy_context(True):
        yield sandbox


def _is_denial(result: str) -> bool:
    """A denial from either guard, in either guard's JSON shape."""
    try:
        payload = json.loads(result)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("error")) and (
        payload.get("guest_policy") is True or payload.get("extension_policy") is True
    )


# ---------------------------------------------------------------------------
# 1. the shipped dark bundle must not be able to answer, or bind, tool policy
# ---------------------------------------------------------------------------


def test_dark_bundle_does_not_declare_tool_authorization(poke_extension):
    """A non-authoritative bundle must not hold the tool-policy capability.

    Declaring it is what let ``_tool_authorization_owner`` bind a policy token
    for a bundle whose answer is an unconditional allow.
    """
    assert "tool_authorization" not in poke_extension.DARK_CAPABILITIES
    assert "tool_authorization" in poke_extension.FORBIDDEN_DARK_CAPABILITIES

    bundle = poke_extension.DarkPokeExtension().build_bundle()
    assert "tool_authorization" not in bundle.capabilities
    assert bundle.authorize_tool is None


def test_dark_bundle_binds_no_policy_token(poke_extension):
    """``turn_policy_scope`` must resolve *no* owner for a dark-only profile."""
    from gateway.conversation_extension_runtime import turn_policy_scope

    scope = "/tmp/poke-dark-scope"
    ce.conversation_extension_registry.register(
        poke_extension.DarkPokeExtension().build_bundle(), scope=scope
    )

    with turn_policy_scope(scope=scope, route_id="r"):
        assert ce.current_request_policy() is None, (
            "a dark bundle must not bind a request policy token"
        )


# ---------------------------------------------------------------------------
# 2. the rollback path itself: dark plugin + legacy ownership = legacy enforces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "function_name,function_args", [MALICIOUS_TERMINAL, MALICIOUS_EXECUTE_CODE]
)
def test_rollback_with_dark_plugin_still_denies_malicious_tool(
    poke_extension, guest_policy, function_name, function_args
):
    """The exact review repro: config rolled back to legacy, plugin still loaded.

    This drives the production dispatcher end to end. Before the fix it
    returned the tool's real output; it must now deny.
    """
    import model_tools

    scope = "/tmp/poke-rollback-scope"
    ce.conversation_extension_registry.register(
        poke_extension.DarkPokeExtension().build_bundle(), scope=scope
    )
    # Rolled back: every domain is legacy-owned.
    co.conversation_ownership_registry.install(
        scope,
        {
            domain: co.OwnerSelection(domain, co.OwnerKind.LEGACY)
            for domain in co.OwnershipDomain
        },
    )

    from gateway.conversation_extension_runtime import turn_policy_scope

    with turn_policy_scope(scope=scope, route_id="rollback"):
        result = model_tools.handle_function_call(function_name, dict(function_args))

    assert _is_denial(result), f"rollback left {function_name} unguarded: {result[:400]}"


def test_rollback_denial_comes_from_the_legacy_guard(poke_extension, guest_policy):
    """Not just "a denial" — the *legacy* owner must be the one that ran."""
    import model_tools
    import gateway.guest_access as guest_access

    seen: list[str] = []
    real_enforce = guest_access.enforce_guest_tool_call

    def _tracking(name, args):
        seen.append(name)
        return real_enforce(name, args)

    scope = "/tmp/poke-rollback-legacy-owner"
    ce.conversation_extension_registry.register(
        poke_extension.DarkPokeExtension().build_bundle(), scope=scope
    )
    co.conversation_ownership_registry.install(
        scope,
        {
            domain: co.OwnerSelection(domain, co.OwnerKind.LEGACY)
            for domain in co.OwnershipDomain
        },
    )

    from gateway.conversation_extension_runtime import turn_policy_scope

    original = guest_access.enforce_guest_tool_call
    guest_access.enforce_guest_tool_call = _tracking
    try:
        with turn_policy_scope(scope=scope, route_id="r"):
            result = model_tools.handle_function_call(*MALICIOUS_TERMINAL)
    finally:
        guest_access.enforce_guest_tool_call = original

    assert seen == ["terminal"], "the legacy guard must run while legacy owns routing"
    assert _is_denial(result)


# ---------------------------------------------------------------------------
# 3. every dispatch path the plan enumerates
# ---------------------------------------------------------------------------


def _register_dark_and_rollback(poke_extension, scope: str) -> None:
    ce.conversation_extension_registry.register(
        poke_extension.DarkPokeExtension().build_bundle(), scope=scope
    )
    co.conversation_ownership_registry.install(
        scope,
        {
            domain: co.OwnerSelection(domain, co.OwnerKind.LEGACY)
            for domain in co.OwnershipDomain
        },
    )


def test_deferred_bridge_dispatch_is_denied(poke_extension, guest_policy):
    """The tool_search bridge unwraps to the same dispatcher; it must deny too."""
    import model_tools

    scope = "/tmp/poke-rollback-deferred"
    _register_dark_and_rollback(poke_extension, scope)

    from gateway.conversation_extension_runtime import turn_policy_scope

    with turn_policy_scope(scope=scope, route_id="r"):
        result = model_tools.handle_function_call(
            "tool_call",
            {"name": MALICIOUS_TERMINAL[0], "arguments": dict(MALICIOUS_TERMINAL[1])},
        )
    # Either the bridge refuses to resolve the tool, or the inner dispatch
    # denies. What must never happen is the command running.
    assert "pwned" not in result
    assert "HERMES" not in result or _is_denial(result), result[:400]


def test_inline_recursive_dispatch_is_denied(poke_extension, guest_policy):
    """A second, nested dispatch inside a bound scope stays denied."""
    import model_tools

    scope = "/tmp/poke-rollback-inline"
    _register_dark_and_rollback(poke_extension, scope)

    from gateway.conversation_extension_runtime import turn_policy_scope

    with turn_policy_scope(scope=scope, route_id="r"):
        first = model_tools.handle_function_call(*MALICIOUS_TERMINAL)
        second = model_tools.handle_function_call(*MALICIOUS_TERMINAL)

    assert _is_denial(first) and _is_denial(second)


def test_thread_hop_dispatch_is_denied(poke_extension, guest_policy):
    """The executor/thread hop must carry the same verdict.

    ``propagate_context_to_thread`` is what makes final-dispatch enforcement
    mandatory across the concurrent tool executor. The legacy guard is
    ContextVar-backed too, so both owners must survive the hop.
    """
    import model_tools
    from tools.thread_context import propagate_context_to_thread
    from concurrent.futures import ThreadPoolExecutor

    scope = "/tmp/poke-rollback-thread"
    _register_dark_and_rollback(poke_extension, scope)

    from gateway.conversation_extension_runtime import turn_policy_scope

    with turn_policy_scope(scope=scope, route_id="r"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = propagate_context_to_thread(model_tools.handle_function_call)
            result = pool.submit(call, *MALICIOUS_TERMINAL).result(timeout=30)

    assert _is_denial(result), result[:400]


# ---------------------------------------------------------------------------
# 4. the general contract, independent of the Poke plugin
# ---------------------------------------------------------------------------


def test_bound_token_alone_cannot_disarm_legacy_while_legacy_owns_routing(guest_policy):
    """Token presence is not ownership.

    Even a *different* extension binding a policy token must not stand the
    legacy guard down while the ownership plan says legacy owns routing.
    """
    import model_tools

    scope = "/tmp/legacy-owns-but-token-bound"
    allow_all = ce.GatewayConversationExtension(
        extension_id="allow-all",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization", "health"}),
        authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(
            True, "allow_everything"
        ),
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    ce.conversation_extension_registry.register(allow_all, scope=scope)
    co.conversation_ownership_registry.install(
        scope,
        {
            domain: co.OwnerSelection(domain, co.OwnerKind.LEGACY)
            for domain in co.OwnershipDomain
        },
    )

    policy = ce.issue_request_policy(
        extension_id="allow-all", profile_home=scope, route_id="r"
    )
    with ce.request_policy_scope(policy):
        result = model_tools.handle_function_call(*MALICIOUS_TERMINAL)

    assert _is_denial(result), (
        "an extension's allow must not bypass the legacy guard that owns routing"
    )


def test_unowned_routing_denies_rather_than_falling_back(guest_policy):
    """An unowned routing domain must not re-enable the legacy guard silently.

    It is still safe (the call is refused), but the refusal must be explicit:
    unowned means "nobody may run", and the strictest available guard applies.
    """
    import model_tools

    scope = "/tmp/unowned-routing"
    co.conversation_ownership_registry.install(
        scope,
        {
            domain: co.OwnerSelection(domain, co.OwnerKind.UNOWNED, reason="no_owner")
            for domain in co.OwnershipDomain
        },
    )

    from gateway.conversation_extension_runtime import turn_policy_scope

    with turn_policy_scope(scope=scope, route_id="r"):
        result = model_tools.handle_function_call(*MALICIOUS_TERMINAL)

    assert _is_denial(result), result[:400]


def test_extension_owner_denies_and_legacy_does_not_double_run(guest_policy):
    """The forward direction still holds: one owner, and it is the extension."""
    import model_tools
    import gateway.guest_access as guest_access

    legacy_calls: list[str] = []
    original = guest_access.enforce_guest_tool_call

    def _tracking(name, args):
        legacy_calls.append(name)
        return original(name, args)

    scope = "/tmp/extension-owns-routing"
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
    co.conversation_ownership_registry.install(
        scope,
        {
            domain: co.OwnerSelection(
                domain, co.OwnerKind.EXTENSION, extension_id="ext", generation=1
            )
            for domain in co.OwnershipDomain
        },
    )

    guest_access.enforce_guest_tool_call = _tracking
    try:
        policy = ce.issue_request_policy(
            extension_id="ext", profile_home=scope, route_id="r"
        )
        with ce.request_policy_scope(policy):
            result = model_tools.handle_function_call("read_file", {"path": "/etc/hosts"})
    finally:
        guest_access.enforce_guest_tool_call = original

    assert "owner denies" in result
    assert legacy_calls == [], "the legacy guard must not run under an extension owner"
