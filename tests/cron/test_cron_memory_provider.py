from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import _cron_job_needs_memory_provider, run_job


_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


def test_memory_toolset_enables_external_provider_for_cron():
    assert _cron_job_needs_memory_provider({"enabled_toolsets": ["memory"]}) is True


def test_mem0_keywords_do_not_implicitly_enable_external_provider_for_cron():
    assert _cron_job_needs_memory_provider({"name": "nightly mem0 harvest"}) is False
    assert _cron_job_needs_memory_provider({"skills": ["hermes__mem0-first-memory-harvest_KM"]}) is False


def test_memory_denylist_overrides_explicit_cron_toolset():
    job = {"enabled_toolsets": ["memory"]}
    assert _cron_job_needs_memory_provider(job, ["memory"]) is False


def test_unrelated_cron_keeps_memory_provider_disabled():
    assert _cron_job_needs_memory_provider({"name": "daily weather", "enabled_toolsets": ["web"]}) is False


@pytest.mark.parametrize(
    ("job", "expected_tools_only"),
    [
        (
            {
                "id": "harvest",
                "name": "nightly mem0 harvest",
                "prompt": "harvest",
                "enabled_toolsets": ["memory"],
            },
            True,
        ),
        ({"id": "weather", "name": "daily weather", "prompt": "forecast"}, False),
    ],
)
def test_run_job_initializes_memory_only_for_explicit_provider_jobs(
    tmp_path, job, expected_tools_only
):
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=MagicMock()), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value=_RUNTIME,
         ), \
         patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent

        success, _output, final_response, error = run_job(job)

    assert success is True, error
    assert final_response == "ok"
    assert mock_agent_cls.call_args.kwargs["skip_memory"] is True
    assert mock_agent_cls.call_args.kwargs["memory_provider_tools_only"] is expected_tools_only
