"""Differential parity: core legacy owner vs. the copied plugin implementation.

This is the real parity oracle for Checkpoint 2. Unlike the plugin repo's
standalone unit tests, this module imports **both** the actual core
implementation (``gateway.guest_access``, ``gateway.proactive_*``) and the
**real** plugin package, then compares their decisions over a frozen corpus.

Rules this harness obeys:

* Read-only. No live database is opened for writing; the proactive comparisons
  operate on temp-directory fixtures created for the test, and the Guest
  comparisons are pure functions over literal inputs.
* Frozen corpus. Inputs are literal fixtures checked in beside the assertions,
  not sampled from live traffic.
* No outbound calls. Nothing here constructs a transport or an adapter.

If the plugin copy ever diverges from the live owner, these fail — which is
the entire point of dark mode.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(
    os.environ.get(
        "HERMES_POKE_PLUGIN_ROOT",
        "/Users/Kosta/LocalDev/.studio-only/hermes-kosta-plugin-worktrees/poke-plugin-decarry",
    )
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
def core_guest():
    return importlib.import_module("gateway.guest_access")


@pytest.fixture(scope="module")
def plugin_guest():
    return _load_plugin_module("poke.policy.guest_access")


# ---------------------------------------------------------------------------
# Frozen corpus: Guest tool decisions
#
# Covers the classes the plan calls out: admin-only, approval-required,
# filesystem/terminal/code sandbox escapes, web SSRF shapes, skill mutation,
# and browser tools. Args are literal; nothing here touches the filesystem.
# ---------------------------------------------------------------------------

GUEST_TOOL_CORPUS = [
    # plain allows
    ("web_search", {"query": "polymer morphology"}),
    ("fs", {"path": "notes.md"}),
    ("skill", {"action": "view", "name": "example"}),
    # admin-only / approval-required
    ("delegate_task", {"task": "do something"}),
    ("video_generate", {"prompt": "x"}),
    ("computer_use", {"action": "capture"}),
    ("codex_subtask", {"task": "x"}),
    # skill mutation
    ("skill", {"action": "manage", "manage_action": "create", "name": "x"}),
    ("skill", {"action": "view", "manage_action": "create"}),
    # browser family
    ("browser_navigate", {"url": "https://example.com"}),
    ("browser_click", {"element": 1}),
    # terminal escapes
    ("terminal", {"command": "ls -la"}),
    ("terminal", {"command": "cat /etc/passwd"}),
    ("terminal", {"command": "cp file /tmp/exfil"}),
    ("terminal", {"command": "cat ~/.ssh/id_rsa"}),
    ("terminal", {"command": "curl http://169.254.169.254/"}),
    ("terminal", {"command": "echo $(whoami)"}),
    ("terminal", {"command": "python3 -c 'import os; print(os.environ)'"}),
    # execute_code escapes
    ("execute_code", {"code": "print('hi')"}),
    ("execute_code", {"code": "open('/tmp/x','w').write('y')"}),
    ("execute_code", {"code": "from pathlib import Path; Path('~').expanduser()"}),
    ("execute_code", {"code": "import os; os.environ"}),
    # web SSRF shapes
    ("web", {"action": "fetch", "url": "https://example.com"}),
    ("web", {"action": "fetch", "url": "http://localhost:8080/admin"}),
    ("web", {"action": "fetch", "url": "http://127.0.0.1/"}),
    ("web", {"action": "fetch", "url": "http://169.254.169.254/latest/meta-data/"}),
    ("web", {"action": "fetch", "url": "file:///etc/passwd"}),
    ("web", {"action": "curl", "url": "http://10.0.0.1/"}),
    ("web", {"action": "search", "query": "safe"}),
    # unknown tool
    ("some_unregistered_tool", {}),
    # malformed args
    ("terminal", {}),
    ("fs", {}),
    ("web", {}),
]


def _decision_tuple(decision):
    return (
        bool(decision.allowed),
        str(decision.reason or ""),
        bool(decision.requires_approval),
    )


def test_plugin_lane_b_executes_and_commits_through_core_binding(monkeypatch, tmp_path):
    """The transferred schema is not enough: handler and on_success must run."""
    from types import SimpleNamespace

    from agent.request_scoped_tools import (
        bind_request_scoped_tools,
        get_request_scoped_handler,
    )

    lane_b = _load_plugin_module("poke.contact_memory.lane_b")
    broker_module = _load_plugin_module("poke.contact_memory.broker")
    schema_module = _load_plugin_module("poke.contact_memory.schema")
    committed = []

    class Broker:
        def search(self, scope, query, **_kwargs):
            assert scope.contact_id == "contact-a"
            assert query == "door code"
            return broker_module.RecallBundle("2468", ("fact-a",))

        def record_usage(self, scope, fact_ids, **kwargs):
            committed.append((scope.contact_id, tuple(fact_ids), kwargs))

    monkeypatch.setattr(lane_b, "get_broker", lambda *_args, **_kwargs: Broker())
    scope = broker_module.RetrievalScope(
        schema_module.RetrievalPrincipal.OWNER,
        "contact-a",
        "session-a",
    )
    tool = lane_b.build_lane_b_tool(
        root=tmp_path, config={}, scope=scope, turn_index=4
    )
    agent = SimpleNamespace(tools=[], valid_tool_names=set())

    with bind_request_scoped_tools(agent, (tool,)) as binding:
        handler = get_request_scoped_handler(agent, "contact_memory_search")
        assert handler is not None
        assert handler({"query": "door code"}) == "2468"
        binding.commit_success()

    assert committed == [
        ("contact-a", ("fact-a",), {"turn_index": 4})
    ]


@pytest.mark.parametrize("function_name,function_args", GUEST_TOOL_CORPUS)
def test_guest_tool_decision_parity(
    core_guest, plugin_guest, function_name, function_args, tmp_path
):
    """The plugin copy must decide identically to the live core owner."""
    sandbox = tmp_path / "guest-sandbox"
    sandbox.mkdir(exist_ok=True)

    core_decision = core_guest.evaluate_guest_tool_call(
        function_name, function_args, sandbox_root=sandbox
    )
    plugin_decision = plugin_guest.evaluate_guest_tool_call(
        function_name, function_args, sandbox_root=sandbox
    )

    assert _decision_tuple(core_decision) == _decision_tuple(plugin_decision), (
        f"divergence for {function_name}({function_args}): "
        f"core={_decision_tuple(core_decision)} plugin={_decision_tuple(plugin_decision)}"
    )


def test_guest_tool_corpus_exercises_both_outcomes(core_guest, tmp_path):
    """Guard the corpus itself: a corpus that only allows proves nothing."""
    sandbox = tmp_path / "s"
    sandbox.mkdir()
    outcomes = {
        core_guest.evaluate_guest_tool_call(name, args, sandbox_root=sandbox).allowed
        for name, args in GUEST_TOOL_CORPUS
    }
    assert outcomes == {True, False}, "corpus must contain both allows and denies"


def test_guest_corpus_covers_approval_required(core_guest, tmp_path):
    sandbox = tmp_path / "s"
    sandbox.mkdir()
    assert any(
        core_guest.evaluate_guest_tool_call(
            name, args, sandbox_root=sandbox
        ).requires_approval
        for name, args in GUEST_TOOL_CORPUS
    )


# ---------------------------------------------------------------------------
# Frozen corpus: BlueBubbles Guest route / admission decisions
# ---------------------------------------------------------------------------


ROUTE_REGISTRY_FIXTURE = {
    "guest_profile": "guest",
    "owner_identities": ["owner@example.com", "+15550000001"],
    "contacts": {
        "contact-a": {
            "id": "contact-a",
            "display_name": "Contact A",
            "role": "family_guest",
            "identities": {
                "bluebubbles": {
                    "handles": ["guest-a@example.com", "+15550000002"]
                }
            },
        }
    },
}


class _Source:
    def __init__(self, user_id, chat_id="chat-1", chat_type="dm", is_group=False):
        self.user_id = user_id
        self.chat_id = chat_id
        self.chat_type = chat_type
        self.is_group = is_group
        self.platform = type("_P", (), {"value": "bluebubbles"})()


ROUTE_CORPUS = [
    # (label, sender identity, raw message)
    ("owner-dm", "owner@example.com", {"handle": {"address": "owner@example.com"}}),
    ("approved-guest-dm", "guest-a@example.com", {"handle": {"address": "guest-a@example.com"}}),
    ("unknown-dm", "stranger@example.com", {"handle": {"address": "stranger@example.com"}}),
    ("owner-by-phone", "+15550000001", {"handle": {"address": "+15550000001"}}),
    ("guest-by-phone", "+15550000002", {"handle": {"address": "+15550000002"}}),
    ("empty-identity", "", {}),
    ("none-message", "guest-a@example.com", None),
]


def _write_registry(path: Path) -> Path:
    import json

    target = path / "contacts.json"
    target.write_text(json.dumps(ROUTE_REGISTRY_FIXTURE), encoding="utf-8")
    return target


def _route_tuple(decision):
    return (
        str(getattr(getattr(decision, "route", None), "value", decision.route)),
        decision.profile,
        decision.contact_id,
        decision.contact_role,
        decision.reason,
    )


@pytest.mark.parametrize("label,identity,raw", ROUTE_CORPUS)
def test_bluebubbles_route_parity(
    core_guest, plugin_guest, label, identity, raw, tmp_path
):
    """Route/admission decisions must be byte-identical across owners."""
    registry_path = _write_registry(tmp_path)
    core_registry = core_guest.load_contact_registry(registry_path)
    plugin_registry = plugin_guest.load_contact_registry(registry_path)

    core_decision = core_guest.classify_bluebubbles_route(
        _Source(identity), raw, core_registry
    )
    plugin_decision = plugin_guest.classify_bluebubbles_route(
        _Source(identity), raw, plugin_registry
    )

    assert _route_tuple(core_decision) == _route_tuple(plugin_decision), (
        f"route divergence for {label}: core={_route_tuple(core_decision)} "
        f"plugin={_route_tuple(plugin_decision)}"
    )


def test_route_corpus_exercises_multiple_routes(core_guest, tmp_path):
    """Guard the route corpus: it must produce more than one route outcome."""
    registry = core_guest.load_contact_registry(_write_registry(tmp_path))
    routes = {
        _route_tuple(
            core_guest.classify_bluebubbles_route(_Source(identity), raw, registry)
        )[0]
        for _label, identity, raw in ROUTE_CORPUS
    }
    assert len(routes) > 1, f"route corpus is degenerate: {routes}"


def test_identity_normalization_parity(core_guest, plugin_guest):
    corpus = [
        "Owner@Example.com",
        " +1 (555) 000-0001 ",
        "+15550000001",
        "guest-a@example.com",
        "",
        None,
        123,
    ]
    for value in corpus:
        assert core_guest.normalize_identity(value) == plugin_guest.normalize_identity(
            value
        ), f"identity normalization diverged for {value!r}"


def test_registry_owner_classification_parity(core_guest, plugin_guest, tmp_path):
    registry_path = _write_registry(tmp_path)
    core_registry = core_guest.load_contact_registry(registry_path)
    plugin_registry = plugin_guest.load_contact_registry(registry_path)

    for identity in (
        "owner@example.com",
        "+15550000001",
        "guest-a@example.com",
        "stranger@example.com",
        "",
    ):
        assert core_registry.is_owner_identity(
            identity
        ) == plugin_registry.is_owner_identity(identity), (
            f"owner classification diverged for {identity!r}"
        )


def test_registry_contact_lookup_parity(core_guest, plugin_guest, tmp_path):
    registry_path = _write_registry(tmp_path)
    core_registry = core_guest.load_contact_registry(registry_path)
    plugin_registry = plugin_guest.load_contact_registry(registry_path)

    for identity in (
        "guest-a@example.com",
        "+15550000002",
        "stranger@example.com",
        "",
    ):
        core_contact = core_registry.find_bluebubbles_contact(identity)
        plugin_contact = plugin_registry.find_bluebubbles_contact(identity)
        core_id = getattr(core_contact, "contact_id", None)
        plugin_id = getattr(plugin_contact, "contact_id", None)
        assert core_id == plugin_id, (
            f"contact lookup diverged for {identity!r}: {core_id} vs {plugin_id}"
        )


# ---------------------------------------------------------------------------
# Tool-decision parity across every dispatch path
# ---------------------------------------------------------------------------


def test_plugin_decisions_match_core_through_the_dispatch_gate(
    core_guest, plugin_guest, tmp_path, monkeypatch
):
    """A denied-by-both tool must be denied by the real dispatcher too.

    This links the pure parity above to the actual enforcement seam: the same
    decision the plugin computes is what the core dispatch gate would apply if
    the plugin were authoritative.
    """
    from gateway import conversation_extensions as ce
    from model_tools import handle_function_call

    scope = str(tmp_path)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    denied = [
        (name, args)
        for name, args in GUEST_TOOL_CORPUS
        if not plugin_guest.evaluate_guest_tool_call(
            name, args, sandbox_root=sandbox
        ).allowed
    ]
    assert denied, "corpus must contain denied calls"

    def _authorize(request):
        decision = plugin_guest.evaluate_guest_tool_call(
            request.function_name, request.function_args, sandbox_root=sandbox
        )
        return ce.GatewayToolAuthorizationDecision(
            decision.allowed, decision.reason, decision.requires_approval
        )

    ce.conversation_extension_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    try:
        ce.conversation_extension_registry.register(
            ce.GatewayConversationExtension(
                extension_id="poke-parity",
                api_version=ce.EXTENSION_API_VERSION,
                capabilities=frozenset({"tool_authorization"}),
                authorize_tool=_authorize,
            ),
            scope=scope,
        )
        policy = ce.issue_request_policy(
            extension_id="poke-parity", profile_home=scope, route_id="r1"
        )
        with ce.request_policy_scope(policy):
            for name, args in denied:
                result = handle_function_call(name, dict(args))
                assert isinstance(result, str)
                assert "extension_policy" in result, (
                    f"{name} was not denied at the dispatch gate: {result[:200]}"
                )
    finally:
        ce.conversation_extension_registry.reset_for_tests()
        ce.reset_request_policy_for_tests()


# ---------------------------------------------------------------------------
# Proactive: pure decision parity over frozen inputs
# ---------------------------------------------------------------------------


def test_proactive_active_window_parity():
    core = importlib.import_module("gateway.proactive_scheduler")
    plugin = _load_plugin_module("poke.proactive.scheduler")

    # (epoch seconds, timezone, active_start, active_end, jitter_key)
    corpus = [
        (1787000000.0, "America/New_York", "09:00", "21:30", "k1"),
        (1787040000.0, "America/New_York", "09:00", "21:30", "k2"),
        (1787083000.0, "America/New_York", "08:00", "20:00", "k3"),
        (1787126000.0, "UTC", "09:00", "21:30", "k4"),
        (1787169000.0, "America/New_York", "22:00", "06:00", "k5"),
        (1787212000.0, "Europe/Belgrade", "09:00", "21:30", ""),
    ]
    for timestamp, tz, start, end, key in corpus:
        results = []
        for implementation in (core, plugin):
            try:
                results.append((
                    "result",
                    implementation.push_into_active_hours(
                        timestamp,
                        timezone_name=tz,
                        active_start=start,
                        active_end=end,
                        jitter_key=key,
                    ),
                ))
            except Exception as exc:
                results.append(("error", type(exc), str(exc)))
        assert results[0] == results[1], (
            f"active-window divergence for {timestamp} {start}-{end} {tz} key={key!r}: "
            f"core={results[0]} plugin={results[1]}"
        )


def test_proactive_dismissive_classifier_parity():
    core = importlib.import_module("gateway.proactive_scheduler")
    plugin = _load_plugin_module("poke.proactive.scheduler")

    corpus = [
        "stop texting me",
        "not now",
        "sure, tell me more",
        "leave me alone",
        "thanks!",
        "",
        "please don't message me again",
        "NO",
    ]
    for text in corpus:
        core_result = bool(core.re_search_dismissive(text))
        plugin_result = bool(plugin.re_search_dismissive(text))
        assert core_result == plugin_result, (
            f"dismissive classifier diverged for {text!r}: "
            f"core={core_result} plugin={plugin_result}"
        )


def test_proactive_inbound_outcome_classifier_parity():
    """``classify_inbound_outcome(send, text)`` over a frozen send fixture."""
    core = importlib.import_module("gateway.proactive_scheduler")
    plugin = _load_plugin_module("poke.proactive.scheduler")

    texts = [
        "yes let's do it",
        "no thanks",
        "stop",
        "maybe later",
        "",
        "who is this?",
        "sounds good, thanks",
    ]

    def _send(module):
        # Build the module's own ProactiveSend from identical literal fields so
        # the comparison is about classifier logic, not object identity.
        return module.ProactiveSend(
            send_id="send-1",
            interest_id="interest-1",
            kind=module.ProactiveSendKind.CHECKIN,
            candidate_json='{"topic":"frozen"}',
            gate_decision=module.GateDecision.SENT,
            gate_reason="frozen_fixture",
            sent_at=1787000000.0,
            outcome=None,
            outcome_at=None,
            created_at=1786999900.0,
        )

    core_send = _send(core)
    plugin_send = _send(plugin)

    for text in texts:
        core_result = core.classify_inbound_outcome(core_send, text)
        plugin_result = plugin.classify_inbound_outcome(plugin_send, text)
        core_value = getattr(core_result, "value", core_result)
        plugin_value = getattr(plugin_result, "value", plugin_result)
        assert core_value == plugin_value, (
            f"outcome classifier diverged for {text!r}: "
            f"core={core_value} plugin={plugin_value}"
        )


def test_proactive_opaque_contact_filename_parity():
    core = importlib.import_module("gateway.proactive_scheduler")
    plugin = _load_plugin_module("poke.proactive.scheduler")

    # contact_id is required by both implementations; the empty case is
    # asserted separately as a shared rejection.
    for contact_id in ("contact-a", "contact-b", "+15550000002", "Ω-unicode"):
        assert core.opaque_contact_filename(contact_id) == plugin.opaque_contact_filename(
            contact_id
        ), f"opaque filename diverged for {contact_id!r}"


def test_proactive_opaque_contact_filename_rejects_empty_in_both():
    core = importlib.import_module("gateway.proactive_scheduler")
    plugin = _load_plugin_module("poke.proactive.scheduler")

    with pytest.raises(Exception):
        core.opaque_contact_filename("")
    with pytest.raises(Exception):
        plugin.opaque_contact_filename("")


# ---------------------------------------------------------------------------
# Contract-surface parity between core and the plugin's standalone stub
# ---------------------------------------------------------------------------


def test_plugin_test_stub_matches_the_real_core_capability_set():
    """The plugin repo tests against a stub; keep the stub honest."""
    from gateway.conversation_extensions import (
        ALL_CAPABILITIES,
        CAPABILITY_FIELDS,
        EXTENSION_API_VERSION,
    )

    stub_path = PLUGIN_ROOT / "tests" / "poke_plugin" / "core_contract_stub.py"
    if not stub_path.exists():
        pytest.skip("plugin contract stub not available")

    import ast

    tree = ast.parse(stub_path.read_text(encoding="utf-8"))
    namespace: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {
                "EXTENSION_API_VERSION",
                "CAPABILITY_FIELDS",
            }:
                namespace[target.id] = ast.literal_eval(node.value)

    assert namespace.get("EXTENSION_API_VERSION") == EXTENSION_API_VERSION
    assert namespace.get("CAPABILITY_FIELDS") == CAPABILITY_FIELDS


def test_real_plugin_bundle_is_accepted_by_the_real_core_contract():
    """End-to-end: the actual plugin bundle validates against actual core."""
    from gateway import conversation_extensions as ce

    poke_extension = _load_plugin_module("poke.extension")
    bundle = poke_extension.DarkPokeExtension().build_bundle()

    assert isinstance(bundle, ce.GatewayConversationExtension)
    assert bundle.capabilities <= ce.ALL_CAPABILITIES
    # Dark mode: no effectful capability, no routing.
    assert not (bundle.capabilities & poke_extension.FORBIDDEN_DARK_CAPABILITIES)
    assert bundle.authorize_route is None
