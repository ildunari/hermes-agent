from types import SimpleNamespace

from agent.auxiliary_client import (
    _CodexCompletionsAdapter,
    _build_call_kwargs,
    _retarget_request_overrides_for_model,
)


def test_build_call_kwargs_merges_request_overrides_and_extra_body():
    kwargs = _build_call_kwargs(
        "openai-codex",
        "gpt-5.5",
        [{"role": "user", "content": "hi"}],
        extra_body={"existing": True},
        request_overrides={
            "service_tier": "priority",
            "extra_body": {"caller": True},
        },
    )

    assert kwargs["service_tier"] == "priority"
    assert kwargs["extra_body"] == {"existing": True, "caller": True}


def test_build_call_kwargs_normalizes_vibeproxy_claude_xhigh_reasoning():
    kwargs = _build_call_kwargs(
        "vibeproxy",
        "claude-opus-4-8",
        [{"role": "user", "content": "hi"}],
        extra_body={"reasoning_effort": "xhigh"},
    )

    assert kwargs["extra_body"] == {"reasoning": {"effort": "xhigh"}}


def test_build_call_kwargs_preserves_non_vibeproxy_top_level_reasoning_effort():
    kwargs = _build_call_kwargs(
        "openai-codex",
        "gpt-5.5",
        [{"role": "user", "content": "hi"}],
        extra_body={"reasoning_effort": "xhigh"},
    )

    assert kwargs["extra_body"] == {"reasoning_effort": "xhigh"}


def test_codex_auxiliary_adapter_forwards_service_tier(monkeypatch):
    captured = {}

    class FakeStream(list):
        def close(self):
            pass

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeStream()

    fake_client = SimpleNamespace(responses=FakeResponses())

    def fake_consume(_event_stream, **_kwargs):
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="ok")],
                )
            ],
            usage=None,
        )

    monkeypatch.setattr("agent.codex_runtime._consume_codex_event_stream", fake_consume)

    response = _CodexCompletionsAdapter(fake_client, "gpt-5.5").create(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
        service_tier="priority",
    )

    assert response.choices[0].message.content == "ok"
    assert captured["service_tier"] == "priority"
    assert captured["stream"] is True


def test_codex_auxiliary_adapter_preserves_xhigh_reasoning(monkeypatch):
    captured = {}

    class FakeStream(list):
        def close(self):
            pass

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeStream()

    fake_client = SimpleNamespace(responses=FakeResponses())

    def fake_consume(_event_stream, **_kwargs):
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="ok")],
                )
            ],
            usage=None,
        )

    monkeypatch.setattr("agent.codex_runtime._consume_codex_event_stream", fake_consume)

    response = _CodexCompletionsAdapter(fake_client, "gpt-5.5").create(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"reasoning": {"effort": "xhigh"}},
    )

    assert response.choices[0].message.content == "ok"
    assert captured["reasoning"]["effort"] == "xhigh"
    assert captured["reasoning"]["summary"] == "auto"
    assert "extra_body" not in captured


def test_retarget_request_overrides_recomputes_fast_mode_for_fallback_model():
    assert _retarget_request_overrides_for_model(
        {"service_tier": "priority"}, "gpt-5.5"
    ) == {"service_tier": "priority"}
    assert _retarget_request_overrides_for_model(
        {"service_tier": "priority", "extra_body": {"keep": True}}, "glm-5.2"
    ) == {"extra_body": {"keep": True}}
