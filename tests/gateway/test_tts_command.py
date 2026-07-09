"""Tests for the /tts gateway slash command."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="***"),
            Platform.DISCORD: PlatformConfig(enabled=True, token="***"),
        }
    )
    runner.adapters = {}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:c1:u1",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "spoken",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )
    return runner


def _make_event(text="/tts summarize the update"):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u1",
            chat_id="c1",
            user_name="tester",
            chat_type="dm",
        ),
        message_id="m1",
    )


class TestGatewayTtsCommand:
    @pytest.mark.asyncio
    async def test_tts_command_dispatches_from_handle_message(self, monkeypatch):
        import gateway.run as gateway_run

        runner = _make_runner()
        event = _make_event("/tts summarize the update")

        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
        monkeypatch.setattr(
            "agent.model_metadata.get_model_context_length",
            lambda *_args, **_kwargs: 100_000,
        )

        runner._handle_tts_command = AsyncMock(return_value="spoken")

        result = await runner._handle_message(event)

        assert result == "spoken"
        runner._handle_tts_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tts_command_loads_voiceover_skill(self):
        runner = _make_runner()
        event = _make_event("/tts summarize the update")

        built = "Loaded Samantha voiceover skill\nThe user has provided the following instruction alongside the skill invocation: summarize the update"
        with patch(
            "agent.skill_commands.resolve_skill_command_key",
            return_value="/moss",
        ) as mock_resolve, patch(
            "agent.skill_commands.build_skill_invocation_message",
            return_value=built,
        ) as mock_build:
            result = await runner._handle_tts_command(event)

        assert result is None
        assert event.text == built
        mock_build.assert_called_once()
        mock_resolve.assert_called_once_with("/moss-samantha-voiceover")
        args, kwargs = mock_build.call_args
        assert args[0] == "/moss"
        assert args[1] == "summarize the update"
        assert "spoken-style audio version" in kwargs["runtime_note"]
        assert "Samantha" in kwargs["runtime_note"]
        assert "deliver the audio file back into the chat" in kwargs["runtime_note"]

    @pytest.mark.asyncio
    async def test_tts_command_without_args_returns_usage(self):
        runner = _make_runner()
        event = _make_event("/tts")

        result = await runner._handle_tts_command(event)

        assert result is not None
        assert result.startswith("Usage: /tts <prompt>")
