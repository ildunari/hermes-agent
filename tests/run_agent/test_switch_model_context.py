"""Tests that switch_model does not inherit stale context_length overrides."""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from agent.context_compressor import ContextCompressor


def _make_agent_with_compressor(config_context_length=None) -> AIAgent:
    """Build a minimal AIAgent with a context_compressor, skipping __init__."""
    agent = AIAgent.__new__(AIAgent)

    # Primary model settings
    agent.model = "primary-model"
    agent.provider = "openrouter"
    agent.base_url = "https://openrouter.ai/api/v1"
    agent.api_key = "sk-primary"
    agent.api_mode = "chat_completions"
    agent.client = MagicMock()
    agent.quiet_mode = True

    # Store the initial config_context_length override used at agent construction.
    agent._config_context_length = config_context_length

    # Context compressor with primary model values
    compressor = ContextCompressor(
        model="primary-model",
        threshold_percent=0.50,
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-primary",
        provider="openrouter",
        quiet_mode=True,
        config_context_length=config_context_length,
    )
    agent.context_compressor = compressor

    # For switch_model
    agent._primary_runtime = {}

    return agent


@patch("agent.model_metadata.get_model_context_length", return_value=131_072)
def test_switch_model_clears_previous_config_context_length(mock_ctx_len):
    """Switching models must not reuse the previous model.context_length override."""
    agent = _make_agent_with_compressor(config_context_length=32_768)
    setattr(agent, "_fallback_activated", True)
    setattr(agent, "_primary_runtime_restorable", False)
    setattr(agent, "_runtime_routing", {"state": "finished", "runtime": {"model": "old-fallback"}})

    assert agent.context_compressor.model == "primary-model"
    assert agent.context_compressor.context_length == 32_768  # From config override

    # Switch model
    agent.switch_model("new-model", "openrouter", api_key="sk-new", base_url="https://openrouter.ai/api/v1")

    # Verify the old config override is not passed to the new model.
    mock_ctx_len.assert_called_once()
    call_kwargs = mock_ctx_len.call_args.kwargs
    assert call_kwargs.get("config_context_length") is None

    # Verify compressor was updated from the newly resolved model metadata.
    assert agent.context_compressor.model == "new-model"
    assert agent.context_compressor.context_length == 131_072
    assert getattr(agent, "_runtime_routing") is None
    assert getattr(agent, "_fallback_activated") is False
    assert getattr(agent, "_primary_runtime_restorable") is True
    assert getattr(agent, "_selected_runtime_identity") == {"model": "new-model", "provider": "openrouter"}


def test_switch_model_without_config_context_length():
    """When switching models without config override, config_context_length should be None."""
    agent = _make_agent_with_compressor(config_context_length=None)

    with patch("agent.model_metadata.get_model_context_length", return_value=128_000) as mock_ctx_len:
        # Switch model
        agent.switch_model("new-model", "openrouter", api_key="sk-new", base_url="https://openrouter.ai/api/v1")

        # Verify get_model_context_length was called with None
        mock_ctx_len.assert_called_once()
        call_kwargs = mock_ctx_len.call_args.kwargs
        assert call_kwargs.get("config_context_length") is None


def test_switch_model_persists_providers_block_context_length():
    """A switch must resolve and persist the target model's provider override."""
    agent = _make_agent_with_compressor(config_context_length=32_768)
    config = {
        "providers": {
            "vibeproxy": {
                "base_url": "http://127.0.0.1:8485/v1",
                "models": {
                    "claude-fable-5": {"context_length": 500_000},
                },
            },
        },
    }

    with (
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("agent.model_metadata.get_model_context_length", return_value=500_000) as mock_ctx_len,
    ):
        agent.switch_model(
            "claude-fable-5",
            "vibeproxy",
            api_key="dummy",
            base_url="http://127.0.0.1:8485/v1",
        )

    assert agent._config_context_length == 500_000
    assert mock_ctx_len.call_args.kwargs["config_context_length"] == 500_000
    assert agent.context_compressor.context_length == 500_000
