from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from agent.request_scoped_tools import (
    RequestScopedTool,
    bind_request_scoped_tools,
    get_effective_tool_names,
    get_effective_tools,
    get_request_scoped_handler,
)
from gateway.config import Platform
from gateway.contact_memory.schema import (
    AssertionType,
    Audience,
    FactProposal,
    MentionPolicy,
)
from gateway.contact_memory.store import ContactMemoryStore
from gateway.run import (
    TrustedContactScope,
    _contact_memory_brokers,
    _contact_memory_lane_b_tools,
)
from gateway.session import SessionSource
from model_tools import get_tool_definitions
from tests.gateway.test_run_cleanup_progress import (
    CleanupCaptureAdapter,
    ProgressAgent,
    _install_fakes,
    _make_runner,
)


def _put(store: ContactMemoryStore, *, logical_id: str, text: str, audience: Audience, policy: MentionPolicy = MentionPolicy.MENTIONABLE) -> None:
    store.supersede_fact(FactProposal(
        logical_id=logical_id,
        subject_id="person:contact",
        predicate="prefers",
        object_text=text,
        audience=audience,
        mention_policy=policy,
        assertion_type=AssertionType.STATED,
        source_id=f"source-{logical_id}",
        source_contact_id="contact-a",
        trust=.99,
        confidence=.99,
    ))


def _seed(root) -> None:
    store = ContactMemoryStore(root / "contact-memory", "contact-a")
    _put(store, logical_id="coffee", text="Their coffee order is a cortado.", audience=Audience.GUEST_OK)
    _put(store, logical_id="door", text="The private door code is 2468.", audience=Audience.OWNER_ONLY)
    _put(
        store,
        logical_id="restricted",
        text="The restricted phrase must never render.",
        audience=Audience.GUEST_OK,
        policy=MentionPolicy.RESTRICTED,
    )


def _tools(root, principal="guest"):
    return _contact_memory_lane_b_tools(
        config_raw={"enabled": True, "lane_b": True},
        trusted_scope=TrustedContactScope(principal, "contact-a"),
        session_key="session-a",
        turn_index=1,
    )


def test_lane_b_is_query_only_request_local_and_not_in_core_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _contact_memory_brokers.clear()
    _seed(tmp_path)
    tools = _tools(tmp_path)
    assert len(tools) == 1
    schema = tools[0].schema
    assert schema["parameters"]["properties"].keys() == {"query"}
    assert schema["parameters"]["additionalProperties"] is False
    assert "contact_memory_search" not in {
        item["function"]["name"] for item in get_tool_definitions(quiet_mode=True)
    }

    agent = SimpleNamespace(tools=[], valid_tool_names=set())
    with bind_request_scoped_tools(agent, tools):
        assert "contact_memory_search" in get_effective_tool_names(agent)
        assert get_effective_tools(agent)[-1]["function"]["name"] == "contact_memory_search"
        assert agent.valid_tool_names == set()
        handler = get_request_scoped_handler(agent, "contact_memory_search")
        assert handler is not None
        assert "cortado" in handler({"query": "coffee order"})
        # Exercise the real agent-runtime dispatch path used by textual and
        # sequential tool calls, not merely the closed-over handler directly.
        from agent.agent_runtime_helpers import invoke_tool
        assert "cortado" in invoke_tool(
            agent,
            "contact_memory_search",
            {"query": "coffee order"},
            "task",
            pre_tool_block_checked=True,
            skip_tool_request_middleware=True,
        )
        assert "accepts only query" in handler({
            "query": "door code",
            "contact_id": "another-contact",
        })
    assert agent.tools == []
    assert agent.valid_tool_names == set()
    assert get_request_scoped_handler(agent, "contact_memory_search") is None


def test_lane_b_applies_broker_audience_and_sensitivity_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _contact_memory_brokers.clear()
    _seed(tmp_path)

    guest_agent = SimpleNamespace(tools=[], valid_tool_names=set())
    with bind_request_scoped_tools(guest_agent, _tools(tmp_path, "guest")):
        search = get_request_scoped_handler(guest_agent, "contact_memory_search")
        assert search is not None
        assert "No visible" in search({"query": "private door code"})
        assert "restricted phrase" not in search({"query": "restricted phrase"})

    owner_agent = SimpleNamespace(tools=[], valid_tool_names=set())
    with bind_request_scoped_tools(owner_agent, _tools(tmp_path, "owner")):
        search = get_request_scoped_handler(owner_agent, "contact_memory_search")
        assert search is not None
        assert "2468" in search({"query": "private door code"})
        assert "restricted phrase" not in search({"query": "restricted phrase"})


@pytest.mark.parametrize("scope", [None, {"principal": "guest", "contact_id": "contact-a"}])
def test_lane_b_absent_without_exact_authenticated_scope(scope):
    # Group, forwarded, and queued gateway paths all deliberately pass None;
    # an attacker-controlled lookalike mapping is rejected as well.
    assert _contact_memory_lane_b_tools(
        config_raw={"enabled": True, "lane_b": True},
        trusted_scope=scope,
        session_key="untrusted",
        turn_index=0,
    ) == []


def test_lane_b_default_off_even_with_trusted_scope():
    scope = TrustedContactScope("guest", "contact-a")
    for config in ({}, {"enabled": True}, {"enabled": True, "lane_b": False}):
        assert _contact_memory_lane_b_tools(
            config_raw=config,
            trusted_scope=scope,
            session_key="session",
            turn_index=0,
        ) == []


@pytest.mark.asyncio
async def test_gateway_e2e_binds_lane_b_for_one_request_then_removes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _contact_memory_brokers.clear()
    _seed(tmp_path)

    class CaptureAgent(ProgressAgent):
        seen_names: list[set[str]] = []
        outputs: list[str | None] = []

        def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
            type(self).seen_names.append(get_effective_tool_names(self))
            handler = get_request_scoped_handler(self, "contact_memory_search")
            type(self).outputs.append(handler({"query": "coffee order"}) if handler else None)
            return {
                "final_response": "done",
                "messages": [{"role": "user", "content": message}],
                "api_calls": 1,
            }

    adapter = CleanupCaptureAdapter(platform=Platform.BLUEBUBBLES)
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, CaptureAgent, cleanup_on=False)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"agent": {"contact_memory": {"enabled": True, "lane_b": True}}},
    )
    source = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id="contact-chat",
        chat_type="dm",
        user_id="trusted-contact",
    )

    first = await runner._run_agent(
        message="what is my coffee order?",
        context_prompt="stable",
        history=[],
        source=source,
        session_id="session-contact",
        session_key="bluebubbles:contact",
        trusted_contact_scope=TrustedContactScope("guest", "contact-a"),
    )
    assert "contact_memory_search" in CaptureAgent.seen_names[-1]
    assert "cortado" in (CaptureAgent.outputs[-1] or "")

    await runner._run_agent(
        message="queued-style follow-up",
        context_prompt="stable",
        history=first["messages"],
        source=source,
        session_id="session-contact",
        session_key="bluebubbles:contact",
        trusted_contact_scope=None,
    )
    assert "contact_memory_search" not in CaptureAgent.seen_names[-1]
    assert CaptureAgent.outputs[-1] is None


def test_overlapping_bindings_on_cached_agent_are_thread_isolated():
    agent = SimpleNamespace(tools=[], valid_tool_names=set())
    barrier = threading.Barrier(2)
    seen: dict[str, tuple[set[str], str]] = {}

    def run(name: str) -> None:
        tool = RequestScopedTool(
            schema={"name": name, "parameters": {"type": "object"}},
            handler=lambda _args: name,
        )
        with bind_request_scoped_tools(agent, [tool]):
            barrier.wait(timeout=2)
            handler = get_request_scoped_handler(agent, name)
            seen[name] = (get_effective_tool_names(agent), handler({}) if handler else "")
            other = "tool_b" if name == "tool_a" else "tool_a"
            assert get_request_scoped_handler(agent, other) is None

    threads = [threading.Thread(target=run, args=(name,)) for name in ("tool_a", "tool_b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert seen == {
        "tool_a": ({"tool_a"}, "tool_a"),
        "tool_b": ({"tool_b"}, "tool_b"),
    }
    assert agent.tools == [] and agent.valid_tool_names == set()


def test_usage_callbacks_commit_only_when_binding_is_marked_successful():
    agent = SimpleNamespace(tools=[], valid_tool_names=set())
    committed: list[tuple[str, ...]] = []
    tool = RequestScopedTool(
        schema={"name": "scoped", "parameters": {"type": "object"}},
        handler=lambda _args: "ok",
        on_success=lambda values: committed.append(tuple(values)),
    )
    from agent.request_scoped_tools import record_request_scoped_usage
    with bind_request_scoped_tools(agent, [tool]):
        record_request_scoped_usage(agent, "scoped", "failed-turn-value")
    assert committed == []
    with bind_request_scoped_tools(agent, [tool]) as binding:
        record_request_scoped_usage(agent, "scoped", "successful-turn-value")
        binding.commit_success()
    assert committed == [("successful-turn-value",)]
