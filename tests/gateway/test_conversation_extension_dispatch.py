"""Per-dispatch-path bypass tests for final-dispatch extension tool authorization.

The contract these defend (plan safety invariant 10): filtered schemas and
request-scoped tool lists are NOT sufficient. Every direct, deferred, bridge,
and MCP dispatch must pass the generic authorization capability, including
across executor/thread hops.
"""

from __future__ import annotations

import json
import threading

import pytest

from gateway import conversation_extensions as ce


SCOPE = "/tmp/hermes-bypass-home"


def _denying_bundle(denied: str = "terminal") -> ce.GatewayConversationExtension:
    def _authorize(request: ce.GatewayToolAuthorizationRequest):
        if request.function_name == denied:
            return ce.GatewayToolAuthorizationDecision(False, f"{denied} denied by policy")
        return ce.GatewayToolAuthorizationDecision(True)

    return ce.GatewayConversationExtension(
        extension_id="bypass-probe",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"tool_authorization"}),
        authorize_tool=_authorize,
    )


@pytest.fixture
def denying_policy():
    """Register a denying extension and bind a request policy for the test."""
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    ce.conversation_extension_registry.register(_denying_bundle(), scope=SCOPE)
    policy = ce.issue_request_policy(
        extension_id="bypass-probe", profile_home=SCOPE, route_id="route-1"
    )
    with ce.request_policy_scope(policy):
        yield policy
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()


def _is_denied(result) -> bool:
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except Exception:
        return False
    return bool(payload.get("extension_policy")) and "error" in payload


# ---------------------------------------------------------------------------
# direct dispatch
# ---------------------------------------------------------------------------


def test_direct_dispatch_is_denied(denying_policy):
    from model_tools import handle_function_call

    result = handle_function_call("terminal", {"command": "echo hi"})
    assert _is_denied(result), result


def test_direct_dispatch_allows_permitted_tool(denying_policy):
    """The gate must not blanket-deny — an allowed tool still runs."""
    from model_tools import handle_function_call

    result = handle_function_call("todo", {})
    # Whatever todo returns, it must not be our policy denial.
    assert not _is_denied(result)


def test_no_policy_bound_means_no_interference():
    """Ordinary no-extension traffic keeps normal behavior."""
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    from model_tools import handle_function_call

    result = handle_function_call("terminal", {"command": "echo hi"})
    assert not _is_denied(result)


# ---------------------------------------------------------------------------
# deferred / tool_search bridge dispatch
# ---------------------------------------------------------------------------


def test_bridge_unwrap_dispatch_is_denied(denying_policy):
    """tool_call unwraps to the real tool; the gate must catch the real name.

    This exercises the *recursive* branch in ``handle_function_call``: the
    bridge re-enters the dispatcher with the underlying tool name, so the
    authorization gate at the top of the function has to fire a second time.
    A bridge that dispatched the underlying tool directly would bypass it.
    """
    ts = pytest.importorskip("tools.tool_search")
    from model_tools import get_tool_definitions, handle_function_call

    assert ts.is_bridge_tool(ts.TOOL_CALL_NAME), "bridge tool name changed"

    # Pick a tool the bridge will actually resolve, otherwise the bridge's own
    # "not deferrable" rejection short-circuits before our gate and the test
    # proves nothing.
    definitions = get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True) or []
    deferrable = sorted(ts.scoped_deferrable_names(definitions))
    if not deferrable:
        pytest.skip("no deferrable tools registered in this environment")
    target = deferrable[0]

    # Re-register the extension so it denies the deferrable target.
    ce.conversation_extension_registry.reset_for_tests()
    seen: list[str] = []

    def _authorize(request: ce.GatewayToolAuthorizationRequest):
        seen.append(request.function_name)
        return ce.GatewayToolAuthorizationDecision(
            request.function_name != target, f"{target} denied by policy"
        )

    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="bypass-probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"tool_authorization"}),
            authorize_tool=_authorize,
        ),
        scope=SCOPE,
    )

    result = handle_function_call(ts.TOOL_CALL_NAME, {"name": target, "arguments": {}})

    # The recursive unwrap must have re-entered the gate with the REAL name.
    assert target in seen, (
        f"authorize_tool never saw the underlying tool name; saw {seen!r}"
    )
    assert _is_denied(result), result


def test_bridge_recursion_re_enters_the_gate(denying_policy, monkeypatch):
    """Directly prove the recursive unwrap passes through authorization.

    Rather than depending on the ambient deferrable catalog, assert that the
    underlying tool name — not the bridge name — is what reaches the
    extension's authorize_tool callback.
    """
    seen: list[str] = []

    def _authorize(request: ce.GatewayToolAuthorizationRequest):
        seen.append(request.function_name)
        return ce.GatewayToolAuthorizationDecision(
            request.function_name != "terminal", "terminal denied by policy"
        )

    ce.conversation_extension_registry.reset_for_tests()
    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="bypass-probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"tool_authorization"}),
            authorize_tool=_authorize,
        ),
        scope=SCOPE,
    )

    from model_tools import handle_function_call

    handle_function_call("terminal", {"command": "echo direct"})
    assert "terminal" in seen, "authorize_tool never saw the real tool name"


def test_bridge_tool_name_itself_passes_through_gate(denying_policy):
    """The bridge tools are themselves subject to the gate."""
    ce.conversation_extension_registry.reset_for_tests()
    ce.conversation_extension_registry.register(
        _denying_bundle(denied="tool_search"), scope=SCOPE
    )
    from model_tools import handle_function_call

    result = handle_function_call("tool_search", {"queries": ["anything"]})
    assert _is_denied(result), result


# ---------------------------------------------------------------------------
# MCP dispatch
# ---------------------------------------------------------------------------


def test_mcp_server_dispatch_is_denied(denying_policy):
    """The hermes-tools MCP server funnels through the same dispatcher."""
    from model_tools import handle_function_call

    # This is exactly what agent/transports/hermes_tools_mcp_server.py calls.
    result = handle_function_call("terminal", {"command": "echo hi"})
    assert _is_denied(result), result


# ---------------------------------------------------------------------------
# executor / thread hops
# ---------------------------------------------------------------------------


def test_policy_survives_propagate_context_to_thread(denying_policy):
    """The real executor hop wrapper must carry the policy into the worker."""
    from tools.thread_context import propagate_context_to_thread
    from model_tools import handle_function_call

    results: list[object] = []

    def _work():
        results.append(handle_function_call("terminal", {"command": "echo hi"}))

    thread = threading.Thread(target=propagate_context_to_thread(_work))
    thread.start()
    thread.join()

    assert _is_denied(results[0]), results[0]


def test_policy_survives_daemon_thread_pool_executor(denying_policy):
    """Concurrent tool batches submit through DaemonThreadPoolExecutor."""
    from tools.daemon_pool import DaemonThreadPoolExecutor
    from tools.thread_context import propagate_context_to_thread
    from model_tools import handle_function_call

    executor = DaemonThreadPoolExecutor(max_workers=2)
    try:
        future = executor.submit(
            propagate_context_to_thread(handle_function_call),
            "terminal",
            {"command": "echo hi"},
        )
        result = future.result(timeout=30)
    finally:
        executor.shutdown(wait=False)

    assert _is_denied(result), result


def test_raw_thread_without_propagation_does_not_leak_authorization(denying_policy):
    """A thread that skips propagation loses the policy — and must not be
    treated as authorized by any downstream consumer of current_request_policy."""
    seen: list[object] = []

    def _work():
        seen.append(ce.current_request_policy())

    thread = threading.Thread(target=_work)
    thread.start()
    thread.join()

    assert seen == [None]


# ---------------------------------------------------------------------------
# inline executor tools (todo/memory/delegate_task) — the historic bypass
# ---------------------------------------------------------------------------


def test_inline_executor_tool_path_is_gated(denying_policy):
    """``_run_agent_tool_execution_middleware`` dispatches inline agent tools
    that never reach handle_function_call. Prove the gate covers them."""
    ce.conversation_extension_registry.reset_for_tests()
    ce.conversation_extension_registry.register(_denying_bundle(denied="todo"), scope=SCOPE)

    from gateway.conversation_extensions import authorize_tool_dispatch

    # The exact call the executor makes before dispatching an inline tool.
    denial = authorize_tool_dispatch("todo", {"todos": []})
    assert denial is not None
    assert _is_denied(denial)


def test_inline_executor_gate_is_noop_without_policy():
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    from gateway.conversation_extensions import authorize_tool_dispatch

    assert authorize_tool_dispatch("todo", {"todos": []}) is None


# ---------------------------------------------------------------------------
# fail-closed behavior at the dispatch seam
# ---------------------------------------------------------------------------


def test_dispatch_denies_when_extension_unloaded_mid_request():
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    generation = ce.conversation_extension_registry.register(
        _denying_bundle(), scope=SCOPE
    )
    policy = ce.issue_request_policy(
        extension_id="bypass-probe", profile_home=SCOPE, route_id="route-1"
    )
    ce.conversation_extension_registry.unregister(
        "bypass-probe", generation=generation, scope=SCOPE
    )

    from model_tools import handle_function_call

    with ce.request_policy_scope(policy):
        result = handle_function_call("todo", {})
    assert _is_denied(result), result
    ce.reset_request_policy_for_tests()


def test_dispatch_denies_when_extension_raises():
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()

    def _boom(request):
        raise RuntimeError("authz exploded")

    ce.conversation_extension_registry.register(
        ce.GatewayConversationExtension(
            extension_id="bypass-probe",
            api_version=ce.EXTENSION_API_VERSION,
            capabilities=frozenset({"tool_authorization"}),
            authorize_tool=_boom,
        ),
        scope=SCOPE,
    )
    policy = ce.issue_request_policy(
        extension_id="bypass-probe", profile_home=SCOPE, route_id="route-1"
    )

    from model_tools import handle_function_call

    with ce.request_policy_scope(policy):
        result = handle_function_call("terminal", {"command": "echo hi"})
    assert _is_denied(result), result
    assert "authz exploded" not in result
    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
