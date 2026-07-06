"""Tests for the /newthread Telegram gateway command."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/newthread", platform=Platform.TELEGRAM, chat_type="dm"):
    source = SessionSource(
        platform=platform,
        user_id="u1",
        chat_id="67890",
        user_name="tester",
        chat_type=chat_type,
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._background_tasks = set()
    runner._session_model_overrides = {}
    runner._pending_newthread_prompts = {}
    runner.session_store = MagicMock()
    runner._session_key_for_source = lambda source: f"key:{source.chat_id}:{source.thread_id or 'root'}"
    return runner


class TestNewThreadCommand:
    @pytest.mark.asyncio
    async def test_rejects_non_telegram(self):
        runner = _make_runner()
        event = _make_event(platform=Platform.DISCORD)
        result = await runner._handle_newthread_command(event)
        assert "only on Telegram" in result

    @pytest.mark.asyncio
    async def test_rejects_channels(self):
        runner = _make_runner()
        event = _make_event(chat_type="channel")
        result = await runner._handle_newthread_command(event)
        assert result is not None
        assert "DMs or groups" in result

    @pytest.mark.asyncio
    async def test_creates_topic_session_and_welcome_message(self):
        runner = _make_runner()
        adapter = SimpleNamespace(
            create_topic=AsyncMock(return_value=2468),
            send=AsyncMock(),
        )
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner.session_store.get_or_create_session.return_value = MagicMock(
            session_id="sess-new",
            session_key="key:67890:2468",
        )

        event = _make_event(text="/newthread Fresh start")
        result = await runner._handle_newthread_command(event)

        adapter.create_topic.assert_awaited_once_with(chat_id=67890, name="Fresh start", persist=True)
        adapter.send.assert_awaited_once_with(
            "67890",
            "✨ New session started here.\n\nTopic: Fresh start\n\nCreated a fresh Hermes session: **Fresh start**.",
            metadata={"thread_id": "2468", "chat_type": "dm"},
        )
        created_source = runner.session_store.get_or_create_session.call_args.args[0]
        assert created_source.thread_id == "2468"
        assert created_source.chat_topic == "Fresh start"
        assert runner.session_store.get_or_create_session.call_args.kwargs["force_new"] is True
        assert result is None

    @pytest.mark.asyncio
    async def test_creates_topic_from_group_source(self):
        runner = _make_runner()
        adapter = SimpleNamespace(
            create_topic=AsyncMock(return_value=1357),
            send=AsyncMock(),
        )
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner.session_store.get_or_create_session.return_value = MagicMock(
            session_id="sess-group",
            session_key="key:67890:1357",
        )

        event = _make_event(text="/newthread Group smoke", chat_type="group")
        result = await runner._handle_newthread_command(event)

        adapter.create_topic.assert_awaited_once_with(chat_id=67890, name="Group smoke", persist=True)
        adapter.send.assert_awaited_once_with(
            "67890",
            "✨ New session started here.\n\nTopic: Group smoke\n\nCreated a fresh Hermes session: **Group smoke**.",
            metadata={"thread_id": "1357", "chat_type": "group"},
        )
        assert runner.session_store.get_or_create_session.call_args.args[0].chat_type == "group"
        assert result is None

    @pytest.mark.asyncio
    async def test_prompts_for_name_when_no_args(self):
        runner = _make_runner()
        adapter = SimpleNamespace(
            prompt_newthread_name=AsyncMock(return_value=SimpleNamespace(message_id="555")),
            send=AsyncMock(),
        )
        runner.adapters = {Platform.TELEGRAM: adapter}

        event = _make_event(text="/newthread")
        event.message_id = "111"
        result = await runner._handle_newthread_command(event)

        assert result is None
        adapter.prompt_newthread_name.assert_awaited_once()
        args, kwargs = adapter.prompt_newthread_name.await_args
        assert args[0] == "67890"
        assert "short label you would recognize later" in args[1]
        assert "Fix Login Timeout" in args[1]
        assert kwargs["reply_to"] == "111"
        assert runner._pending_newthread_prompts["key:67890:root"]["prompt_message_id"] == "555"

    @pytest.mark.asyncio
    async def test_pending_reply_creates_named_thread_and_sets_session_title(self):
        runner = _make_runner()
        runner._pending_newthread_prompts["key:67890:root"] = {"prompt_message_id": "555"}
        adapter = SimpleNamespace(
            create_topic=AsyncMock(return_value=2468),
            send=AsyncMock(),
        )
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner.session_store.get_or_create_session.return_value = MagicMock(
            session_id="sess-new",
            session_key="key:67890:2468",
        )
        runner._session_db = MagicMock()
        runner._session_db.get_next_title_in_lineage.side_effect = lambda title: title
        runner._session_db.set_session_title.return_value = True

        event = _make_event(text="Lab planning")
        result = await runner._handle_pending_newthread_name(event, "key:67890:root")

        assert result is None
        adapter.create_topic.assert_awaited_once_with(chat_id=67890, name="Lab planning", persist=True)
        runner._session_db.create_session.assert_called_once()
        runner._session_db.set_session_title.assert_called_once_with("sess-new", "Lab planning")
        assert "key:67890:root" not in runner._pending_newthread_prompts

    @pytest.mark.asyncio
    async def test_explains_botfather_private_topic_permission_failure(self):
        runner = _make_runner()
        adapter = SimpleNamespace(
            create_topic=AsyncMock(return_value=None),
            send=AsyncMock(),
            last_topic_create_error="Bot_forum_create_forbidden",
        )
        runner.adapters = {Platform.TELEGRAM: adapter}

        event = _make_event(text="/newthread Failing topic")
        result = await runner._handle_newthread_command(event)

        assert "not allowed" in result
        assert "@BotFather" in result
        assert "private-chat topic" in result
        adapter.send.assert_not_called()
