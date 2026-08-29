"""The generic extension seam enforces tool policy at final dispatch.

The policy decision is mandatory on every dispatch path the gateway uses:
direct, deferred tool-search bridge, recursive bridge unwrap, MCP dispatch,
inline/nested dispatch, and executor/thread propagation. No product-specific
fallback participates in these tests or in production dispatch.

Coverage note: the *deny* direction is the security property. The *allow*
direction is asserted too, because a guard that denies everything would pass a
deny-only suite while breaking every ordinary tool call.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway import conversation_extensions as ce
from gateway import conversation_ownership as co


SENSITIVE = ("terminal", {"command": "cat /etc/passwd", "workdir": "/"})
SENSITIVE_CODE = (
    "execute_code",
    {"code": "import pathlib;print(pathlib.Path('/etc/passwd').read_text())"},
)


@pytest.fixture(autouse=True)
def _clean():
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    co.conversation_ownership_registry.reset_for_tests()
    yield
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    co.conversation_ownership_registry.reset_for_tests()


def _denying_extension(extension_id: str = "acme-policy"):
    """A generic third-party extension that denies sensitive tools."""
    denied = {"terminal", "execute_code", "write_file"}

    def authorize(request: ce.GatewayToolAuthorizationRequest):
        if request.function_name in denied:
            return ce.GatewayToolAuthorizationDecision(
                False, f"{request.function_name} denied by {extension_id}"
            )
        return ce.GatewayToolAuthorizationDecision(True, "allowed")

    return ce.GatewayConversationExtension(
        extension_id=extension_id,
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization", "health"}),
        authorize_tool=authorize,
        health=lambda: ce.GatewayExtensionHealth(True),
    )


def _register(scope: str, bundle=None, *, owner=True):
    bundle = bundle or _denying_extension()
    ce.conversation_extension_registry.register(bundle, scope=scope)
    kind = co.OwnerKind.EXTENSION if owner else co.OwnerKind.CORE
    co.conversation_ownership_registry.install(
        scope,
        {
            domain: co.OwnerSelection(
                domain,
                kind,
                extension_id=bundle.extension_id if owner else None,
                generation=1 if owner else None,
            )
            for domain in co.OwnershipDomain
        },
    )
    return bundle


def _is_denial(result) -> bool:
    try:
        payload = json.loads(result)
    except Exception:
        return False
    return isinstance(payload, dict) and bool(payload.get("error"))


def _policy_scope(scope: str):
    from gateway.conversation_extension_runtime import turn_policy_scope

    return turn_policy_scope(scope=scope, route_id="route-1")


# ---------------------------------------------------------------------------
# 1. direct dispatch
# ---------------------------------------------------------------------------


def test_direct_dispatch_is_denied_by_generic_seam(tmp_path):
    import model_tools

    scope = str(tmp_path / "acme")
    _register(scope)

    with _policy_scope(scope):
        result = model_tools.handle_function_call(*SENSITIVE)

    assert _is_denial(result), result[:400]
    assert "denied by acme-policy" in result


def test_direct_dispatch_allows_permitted_tool(tmp_path):
    """The seam must not be a blanket deny; the owner's allow must pass."""
    import model_tools

    scope = str(tmp_path / "acme")
    _register(scope)
    target = tmp_path / "readable.txt"
    target.write_text("hello\n")

    with _policy_scope(scope):
        result = model_tools.handle_function_call("read_file", {"path": str(target)})

    assert "hello" in result


# ---------------------------------------------------------------------------
# 2. deferred / tool-search bridge dispatch
# ---------------------------------------------------------------------------


def test_deferred_bridge_dispatch_is_denied_by_generic_seam(tmp_path):
    """``tool_call`` unwraps to the real tool; the seam must still deny."""
    import model_tools

    scope = str(tmp_path / "acme")
    _register(scope)

    with _policy_scope(scope):
        result = model_tools.handle_function_call(
            "tool_call", {"name": SENSITIVE[0], "arguments": dict(SENSITIVE[1])}
        )

    assert "root:" not in result, "the sensitive command must never execute"
    assert _is_denial(result), result[:400]


def test_bridge_tool_itself_is_subject_to_the_seam(tmp_path):
    """An extension that denies the bridge name blocks deferred dispatch."""
    import model_tools

    scope = str(tmp_path / "acme")
    bundle = ce.GatewayConversationExtension(
        extension_id="deny-bridge",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization", "health"}),
        authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(
            request.function_name != "tool_call", "bridge denied"
        ),
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    _register(scope, bundle)

    with _policy_scope(scope):
        result = model_tools.handle_function_call(
            "tool_call", {"name": "read_file", "arguments": {"path": "/etc/hosts"}}
        )

    assert _is_denial(result), result[:400]


# ---------------------------------------------------------------------------
# 3. MCP-server dispatch
# ---------------------------------------------------------------------------


def test_mcp_dispatch_is_denied_by_generic_seam(tmp_path):
    """MCP tools funnel through the same dispatcher and must be authorized."""
    import model_tools

    scope = str(tmp_path / "acme")
    bundle = ce.GatewayConversationExtension(
        extension_id="deny-mcp",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization", "health"}),
        authorize_tool=lambda request: ce.GatewayToolAuthorizationDecision(
            not request.function_name.startswith("mcp__"), "mcp denied"
        ),
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    _register(scope, bundle)

    with _policy_scope(scope):
        result = model_tools.handle_function_call(
            "mcp__evil__exfiltrate", {"path": "/etc/passwd"}
        )

    assert _is_denial(result), result[:400]
    assert "mcp denied" in result


# ---------------------------------------------------------------------------
# 4. inline / recursive dispatch
# ---------------------------------------------------------------------------


def test_inline_recursive_dispatch_stays_denied(tmp_path):
    import model_tools

    scope = str(tmp_path / "acme")
    _register(scope)

    with _policy_scope(scope):
        first = model_tools.handle_function_call(*SENSITIVE)
        second = model_tools.handle_function_call(*SENSITIVE_CODE)

    assert _is_denial(first) and _is_denial(second)


# ---------------------------------------------------------------------------
# 5. executor / thread hop
# ---------------------------------------------------------------------------


def test_thread_hop_carries_the_generic_policy(tmp_path):
    import model_tools
    from tools.thread_context import propagate_context_to_thread

    scope = str(tmp_path / "acme")
    _register(scope)

    with _policy_scope(scope):
        with ThreadPoolExecutor(max_workers=2) as pool:
            call = propagate_context_to_thread(model_tools.handle_function_call)
            results = [
                pool.submit(call, *SENSITIVE).result(timeout=60),
                pool.submit(call, *SENSITIVE_CODE).result(timeout=60),
            ]

    assert all(_is_denial(r) for r in results), results


def test_thread_without_propagation_does_not_inherit_policy(tmp_path):
    """Documents why propagation is mandatory rather than optional.

    A raw thread does not inherit the ContextVar, so no policy binds there.
    This is why ``propagate_context_to_thread`` is used by the real executors;
    the assertion pins the contract so a regression in propagation is visible.
    """
    scope = str(tmp_path / "acme")
    _register(scope)

    seen = []
    with _policy_scope(scope):
        with ThreadPoolExecutor(max_workers=1) as pool:
            seen.append(pool.submit(ce.current_request_policy).result(timeout=30))
        assert ce.current_request_policy() is not None

    assert seen == [None]


# ---------------------------------------------------------------------------
# 6. fail-closed properties (no Kosta-specific fallback left to catch these)
# ---------------------------------------------------------------------------


def test_vanished_extension_denies(tmp_path):
    import model_tools

    scope = str(tmp_path / "acme")
    bundle = _register(scope)
    policy = ce.issue_request_policy(
        extension_id=bundle.extension_id, profile_home=scope, route_id="r"
    )
    ce.conversation_extension_registry.reset_for_tests()

    with ce.request_policy_scope(policy):
        result = model_tools.handle_function_call("read_file", {"path": "/etc/hosts"})

    assert _is_denial(result), result[:400]


def test_raising_authorizer_denies(tmp_path):
    import model_tools

    scope = str(tmp_path / "acme")

    def _boom(request):
        raise RuntimeError("authorizer exploded")

    bundle = ce.GatewayConversationExtension(
        extension_id="boom",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization", "health"}),
        authorize_tool=_boom,
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    _register(scope, bundle)

    with _policy_scope(scope):
        result = model_tools.handle_function_call("read_file", {"path": "/etc/hosts"})

    assert _is_denial(result), result[:400]


def test_malformed_decision_denies(tmp_path):
    import model_tools

    scope = str(tmp_path / "acme")
    bundle = ce.GatewayConversationExtension(
        extension_id="malformed",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization", "health"}),
        authorize_tool=lambda request: {"allowed": True},
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    _register(scope, bundle)

    with _policy_scope(scope):
        result = model_tools.handle_function_call("read_file", {"path": "/etc/hosts"})

    assert _is_denial(result), result[:400]


def test_ambiguous_owner_refuses_the_turn(tmp_path):
    """Two authorizers is unresolvable and must fail closed at scope entry."""
    from gateway.conversation_extension_runtime import AmbiguousToolAuthorizationOwner

    scope = str(tmp_path / "acme")
    ce.conversation_extension_registry.register(
        _denying_extension("first"), scope=scope
    )
    ce.conversation_extension_registry.register(
        _denying_extension("second"), scope=scope
    )

    with pytest.raises(AmbiguousToolAuthorizationOwner):
        _policy_scope(scope)


# ---------------------------------------------------------------------------
# 7. no-extension traffic is completely unaffected
# ---------------------------------------------------------------------------


def test_no_extension_no_policy_leaves_dispatch_untouched(tmp_path):
    """Ordinary Hermes: no registered extension, no token, no interference."""
    import model_tools

    scope = str(tmp_path / "plain")
    target = tmp_path / "plain.txt"
    target.write_text("plain-content\n")

    with _policy_scope(scope):
        assert ce.current_request_policy() is None
        result = model_tools.handle_function_call("read_file", {"path": str(target)})

    assert "plain-content" in result


def test_policy_does_not_leak_across_profile_scopes(tmp_path):
    """A policy bound for one profile must not govern another profile."""
    import model_tools

    scope_a = str(tmp_path / "a")
    _register(scope_a)
    scope_b = str(tmp_path / "b")
    target = tmp_path / "b.txt"
    target.write_text("b-content\n")

    with _policy_scope(scope_b):
        assert ce.current_request_policy() is None
        result = model_tools.handle_function_call("read_file", {"path": str(target)})

    assert "b-content" in result
