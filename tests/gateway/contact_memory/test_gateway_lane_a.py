from __future__ import annotations

from agent.prompt_caching import apply_anthropic_cache_control
from gateway.contact_memory.schema import (
    AssertionType, Audience, FactProposal, MentionPolicy,
)
from gateway.contact_memory.store import ContactMemoryStore
from gateway.run import (
    _compile_contact_memory_candidate,
    _compile_contact_memory_prompt,
    _contact_memory_brokers,
)


def _seed(root):
    store = ContactMemoryStore(root / "contact-memory", "contact-a")
    store.supersede_fact(FactProposal(
        logical_id="car", subject_id="person:contact", predicate="owns",
        object_text="The car under discussion is the green hatchback.",
        audience=Audience.GUEST_OK, mention_policy=MentionPolicy.MENTIONABLE,
        assertion_type=AssertionType.STATED, source_id="synthetic-1",
        source_contact_id="contact-a", trust=.95, confidence=.95,
    ))


def test_gateway_lane_a_is_default_off_and_requires_trusted_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _contact_memory_brokers.clear()
    _seed(tmp_path)
    args = dict(
        message="should i keep the car", history=[], session_key="session",
        now_ts=1000.0, texture_prompt="response_class: answer",
    )
    assert _compile_contact_memory_prompt(config_raw={}, trusted_scope=None, **args) == ""
    enabled = {"enabled": True, "lane_a": True}
    assert _compile_contact_memory_prompt(config_raw=enabled, trusted_scope=None, **args) == ""
    # Even apparently trusted scope cannot bypass the cache-safety block.
    assert _compile_contact_memory_prompt(
        config_raw=enabled,
        trusted_scope={"principal": "guest", "session_contact_id": "contact-a"},
        **args,
    ) == ""
    # Offline candidate remains testable without entering provider assembly.
    rendered = _compile_contact_memory_candidate(
        config_raw=enabled,
        trusted_scope={"principal": "guest", "session_contact_id": "contact-a"},
        **args,
    )
    assert "green hatchback" in rendered


def test_recall_is_blocked_before_actual_cache_marked_message_assembly(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _contact_memory_brokers.clear()
    _seed(tmp_path)
    base = "stable profile prompt"
    config = {"enabled": True, "lane_a": True}
    recalls = [
        _compile_contact_memory_prompt(
            config_raw=config,
            trusted_scope={"principal": "guest", "session_contact_id": "contact-a"},
            message=message,
            history=[],
            session_key="session",
            now_ts=now,
        )
        for message, now in (("should i keep the car", 1000.0), ("should i sell the car", 1200.0))
    ]
    assembled = []
    for recall in recalls:
        effective_system = (base + "\n\n" + recall).strip() if recall else base
        marked = apply_anthropic_cache_control([
            {"role": "system", "content": effective_system},
            {"role": "user", "content": "turn"},
        ], cache_ttl="1h")
        assembled.append(marked[0]["content"])
    assert recalls == ["", ""]
    assert assembled[0] == assembled[1]
    assert assembled[0][0]["text"] == base
    assert assembled[0][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_hostile_or_approved_group_scope_cannot_enable_lane_a():
    config = {"enabled": True, "lane_a": True}
    common = dict(
        config_raw=config, message="should i keep the car", history=[],
        session_key="session", now_ts=1000.0,
    )
    # Adapter-forged scope and approved-group authorization both remain inert.
    assert _compile_contact_memory_prompt(
        trusted_scope={"principal": "guest", "session_contact_id": "../../owner"}, **common
    ) == ""
    assert _compile_contact_memory_prompt(trusted_scope=None, **common) == ""


def test_gateway_lane_a_fails_open_on_backend_error(monkeypatch):
    from gateway.contact_memory.broker import ContactMemoryBroker
    _contact_memory_brokers.clear()

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic corrupt store")

    monkeypatch.setattr(ContactMemoryBroker, "prefetch", explode)
    rendered = _compile_contact_memory_candidate(
        config_raw={"enabled": True, "lane_a": True},
        trusted_scope={"principal": "guest", "session_contact_id": "contact-a"},
        message="should i keep it", history=[], session_key="session",
        now_ts=1000.0,
    )
    assert rendered == ""
