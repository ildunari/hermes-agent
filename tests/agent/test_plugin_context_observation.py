from __future__ import annotations

import hashlib
from types import SimpleNamespace

from agent.turn_context import (
    _collect_pre_llm_call_context,
    bind_plugin_context_observer,
    reset_plugin_context_observer,
)
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest


def _manager_with_hooks(*callbacks):
    manager = PluginManager()
    context = PluginContext(
        PluginManifest(name="Conversation Texture", key="conversation-texture"), manager)
    for callback in callbacks:
        context.register_hook("pre_llm_call", callback)
    return manager


def _collect(monkeypatch, manager, *, session_id="runtime-1", turn_id="turn-1"):
    monkeypatch.setattr("hermes_cli.plugins._delivery_manager", lambda: manager)
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0)
    agent = SimpleNamespace(session_id=session_id, model="model", platform="ios")
    return _collect_pre_llm_call_context(
        agent,
        effective_task_id="task-1",
        turn_id=turn_id,
        original_user_message="credential prompt must stay private",
        messages=[{"role": "user", "content": "credential prompt must stay private"}],
        conversation_history=None,
    )


def test_observation_is_sanitized_and_context_return_is_unchanged(monkeypatch):
    context = "Texture π with TEST_RUNNER_PASSWORD=private-value"
    manager = _manager_with_hooks(lambda **_kwargs: {"context": context})
    observed = []
    token = bind_plugin_context_observer(observed.append)
    try:
        result = _collect(monkeypatch, manager)
    finally:
        reset_plugin_context_observer(token)

    assert result == context
    assert observed == [{
        "plugin_id": "conversation-texture",
        "hook": "pre_llm_call",
        "platform": "ios",
        "runtime_session_id": "runtime-1",
        "turn_id": "turn-1",
        "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "context_length": len(context.encode("utf-8")),
    }]
    serialized = repr(observed)
    assert context not in serialized
    assert "private-value" not in serialized
    assert "credential prompt" not in serialized


def test_empty_and_failed_hooks_emit_no_observation(monkeypatch):
    def failed(**_kwargs):
        raise RuntimeError("private failure detail")

    manager = _manager_with_hooks(
        lambda **_kwargs: "",
        lambda **_kwargs: {"context": ""},
        failed,
    )
    observed = []
    token = bind_plugin_context_observer(observed.append)
    try:
        result = _collect(monkeypatch, manager)
    finally:
        reset_plugin_context_observer(token)

    assert result == ""
    assert observed == []


def test_observation_is_suppressed_without_opt_in(monkeypatch):
    manager = _manager_with_hooks(lambda **_kwargs: "texture context")
    assert _collect(monkeypatch, manager) == "texture context"


def test_observer_failure_does_not_change_hook_context(monkeypatch):
    context = "still reaches the model"
    manager = _manager_with_hooks(lambda **_kwargs: {"context": context})

    def observer(_metadata):
        raise RuntimeError("diagnostic sink unavailable")

    token = bind_plugin_context_observer(observer)
    try:
        assert _collect(monkeypatch, manager) == context
    finally:
        reset_plugin_context_observer(token)


def test_unencodable_context_still_reaches_existing_hook_path(monkeypatch):
    context = "surrogate-\ud800-context"
    manager = _manager_with_hooks(lambda **_kwargs: context)
    observed = []
    token = bind_plugin_context_observer(observed.append)
    try:
        assert _collect(monkeypatch, manager) == context
    finally:
        reset_plugin_context_observer(token)
    assert observed == []
