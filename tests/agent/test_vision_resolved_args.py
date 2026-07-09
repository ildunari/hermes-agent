"""Test that call_llm vision path passes resolved provider args, not raw ones."""

from unittest.mock import patch, MagicMock


def test_vision_call_uses_resolved_provider_args():
    """Resolved provider/model/key/url from config must reach resolve_vision_provider_client."""
    from agent.auxiliary_client import call_llm

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="description"))],
        usage=MagicMock(prompt_tokens=10, completion_tokens=5),
    )

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("my-resolved-provider", "my-resolved-model", "http://resolved", "resolved-key", "chat_completions"),
    ), patch(
        "agent.auxiliary_client.resolve_vision_provider_client",
        return_value=("my-resolved-provider", fake_client, "my-resolved-model"),
    ) as mock_vision:
        call_llm(
            "vision",
            provider="raw-provider",
            model="raw-model",
            base_url="http://raw",
            api_key="raw-key",
            messages=[{"role": "user", "content": "describe this"}],
        )

    # The resolved values must be passed, not the raw call_llm arguments
    call_args = mock_vision.call_args
    assert call_args.kwargs["provider"] == "my-resolved-provider"
    assert call_args.kwargs["model"] == "my-resolved-model"
    assert call_args.kwargs["base_url"] == "http://resolved"
    assert call_args.kwargs["api_key"] == "resolved-key"


def test_vision_base_url_override_keeps_explicit_provider():
    """Explicit provider should still drive credential resolution with custom base_url."""
    from agent.auxiliary_client import resolve_vision_provider_client

    fake_client = MagicMock()
    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=(
            "zai",
            "glm-4v",
            "https://open.bigmodel.cn/api/paas/v4",
            None,
            "chat_completions",
        ),
    ), patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(fake_client, "glm-4v"),
    ) as mock_resolve:
        provider, client, model = resolve_vision_provider_client()

    assert provider == "zai"
    assert client is fake_client
    assert model == "glm-4v"
    assert mock_resolve.call_args.args[0] == "zai"
    assert mock_resolve.call_args.kwargs["explicit_base_url"] == "https://open.bigmodel.cn/api/paas/v4"


def test_codex_vision_replaces_foreign_model_hint_with_configured_codex_model(monkeypatch):
    """A stale GLM model hint must not be sent to the ChatGPT/Codex endpoint."""
    from agent.auxiliary_client import resolve_vision_provider_client

    monkeypatch.setattr(
        "agent.auxiliary_client._resolve_task_provider_model",
        lambda *args, **kwargs: ("openai-codex", "glm-5v-turbo", None, None, None),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._get_auxiliary_task_config",
        lambda task: {"provider": "openai-codex", "model": "gpt-5.5"} if task == "vision" else {},
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._read_codex_access_token",
        lambda: "codex-test-token",
    )

    provider, client, model = resolve_vision_provider_client()

    assert provider == "openai-codex"
    assert client is not None
    assert model == "gpt-5.5"


def test_codex_vision_refuses_foreign_model_when_no_safe_replacement(monkeypatch):
    """If no Codex-safe model exists, fail closed instead of sending GLM to Codex."""
    from agent.auxiliary_client import resolve_provider_client

    monkeypatch.setattr("agent.auxiliary_client._get_auxiliary_task_config", lambda task: {})
    monkeypatch.setattr("agent.auxiliary_client._read_main_model", lambda: "glm-5v-turbo")
    monkeypatch.setattr("agent.auxiliary_client._read_codex_access_token", lambda: "codex-test-token")

    client, model = resolve_provider_client("openai-codex", "glm-5v-turbo", task="vision")

    assert client is None
    assert model is None
