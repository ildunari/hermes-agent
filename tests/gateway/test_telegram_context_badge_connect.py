"""Tests for Telegram context badges keeping a Connect button."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    """Wire up the minimal mocks required to import TelegramAdapter."""
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter


class DummyWebAppInfo:
    def __init__(self, url):
        self.url = url


class DummyInlineKeyboardButton:
    def __init__(self, text=None, callback_data=None, web_app=None, url=None):
        self.text = text
        self.callback_data = callback_data
        self.web_app = web_app
        self.url = url


class DummyInlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class TestTelegramContextBadgeConnectButton:
    @pytest.mark.asyncio
    async def test_attach_context_badge_adds_connect_button_from_chat_menu_web_app(self):
        adapter = _make_adapter()
        adapter._bot.get_chat_menu_button = AsyncMock(
            return_value=SimpleNamespace(
                type="web_app",
                web_app=SimpleNamespace(url="https://example.com/miniapp/index.html"),
            )
        )
        adapter._bot.edit_message_reply_markup = AsyncMock()

        with patch("gateway.platforms.telegram.InlineKeyboardButton", DummyInlineKeyboardButton), patch(
            "gateway.platforms.telegram.InlineKeyboardMarkup", DummyInlineKeyboardMarkup
        ), patch("gateway.platforms.telegram.WebAppInfo", DummyWebAppInfo):
            ok = await adapter.attach_context_badge(
                chat_id="12345",
                message_id="42",
                used=1234,
                total=5678,
                details={"prompt": 400, "completion": 200},
                session_key="agent:main:telegram:dm:12345:77",
            )

        assert ok is True
        adapter._bot.get_chat_menu_button.assert_awaited_once_with(chat_id=12345)
        adapter._bot.edit_message_reply_markup.assert_awaited_once()
        kwargs = adapter._bot.edit_message_reply_markup.call_args.kwargs
        markup = kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 2
        assert markup.inline_keyboard[0][0].text == "Connect"
        assert markup.inline_keyboard[0][0].web_app.url == "https://example.com/miniapp/index.html"
        assert markup.inline_keyboard[1][1].text == "ⓘ"
        assert markup.inline_keyboard[1][1].callback_data == "ctxd:42"
        assert adapter._context_badge_sessions["12345:42"] == "agent:main:telegram:dm:12345:77"
        assert adapter._context_badge_details["12345:42"]["prompt"] == 400
        assert adapter._context_badge_details["12345:42"]["used"] == 1234

    @pytest.mark.asyncio
    async def test_attach_context_badge_uses_default_chat_menu_web_app_fallback(self):
        adapter = _make_adapter()
        adapter._bot.get_chat_menu_button = AsyncMock(
            side_effect=[
                SimpleNamespace(type="commands"),
                SimpleNamespace(
                    type="web_app",
                    web_app=SimpleNamespace(url="https://example.com/default-miniapp.html"),
                ),
            ]
        )
        adapter._bot.edit_message_reply_markup = AsyncMock()

        with patch("gateway.platforms.telegram.InlineKeyboardButton", DummyInlineKeyboardButton), patch(
            "gateway.platforms.telegram.InlineKeyboardMarkup", DummyInlineKeyboardMarkup
        ), patch("gateway.platforms.telegram.WebAppInfo", DummyWebAppInfo):
            ok = await adapter.attach_context_badge(
                chat_id="12345",
                message_id="43",
                used=1200,
                total=2400,
            )

        assert ok is True
        assert adapter._bot.get_chat_menu_button.await_args_list[0].kwargs == {"chat_id": 12345}
        assert adapter._bot.get_chat_menu_button.await_args_list[1].kwargs == {}
        markup = adapter._bot.edit_message_reply_markup.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == "Connect"
        assert markup.inline_keyboard[0][0].web_app.url == "https://example.com/default-miniapp.html"

    def test_context_badge_alert_prioritizes_runtime_session_fields(self):
        adapter = _make_adapter()

        alert = adapter._format_context_badge_alert(
            {
                "profile": "coding",
                "provider": "openai-codex",
                "model": "openai/gpt-5.5",
                "reasoning": {"enabled": True, "effort": "high"},
                "cwd": "/Users/Kosta/LocalDev/AgentGlassKit",
                "used": 208_971,
                "total": 300_000,
                "api_calls": 10,
                "compressions": 1,
                "session_id": "0123456789abcdef",
            }
        )

        assert len(alert) <= 200
        assert "Profile: coding" in alert
        assert "Model: openai-codex/gpt-5.5" in alert
        assert "Reasoning: high" in alert
        assert "CWD: ~/LocalDev/AgentGlassKit" in alert
        assert "Context: 209k/300k (70%)" in alert

    def test_context_badge_details_are_scoped_by_chat_and_message(self):
        adapter = _make_adapter()

        adapter._remember_context_badge_details(
            "111",
            42,
            used=10,
            total=100,
            details={"profile": "default", "cwd": "/Users/Kosta"},
            session_key="session-a",
        )
        adapter._remember_context_badge_details(
            "222",
            42,
            used=90,
            total=100,
            details={"profile": "coding", "cwd": "/Users/Kosta/LocalDev"},
            session_key="session-b",
        )

        assert adapter._context_badge_details["111:42"]["profile"] == "default"
        assert adapter._context_badge_details["222:42"]["profile"] == "coding"
        assert adapter._context_badge_sessions["111:42"] == "session-a"
        assert adapter._context_badge_sessions["222:42"] == "session-b"

    @pytest.mark.asyncio
    async def test_attach_context_badge_keeps_existing_two_button_row_when_no_web_app(self):
        adapter = _make_adapter()
        adapter._bot.get_chat_menu_button = AsyncMock(return_value=SimpleNamespace(type="commands"))
        adapter._bot.edit_message_reply_markup = AsyncMock()

        with patch("gateway.platforms.telegram.InlineKeyboardButton", DummyInlineKeyboardButton), patch(
            "gateway.platforms.telegram.InlineKeyboardMarkup", DummyInlineKeyboardMarkup
        ), patch("gateway.platforms.telegram.WebAppInfo", DummyWebAppInfo):
            ok = await adapter.attach_context_badge(
                chat_id="12345",
                message_id="99",
                used=50,
                total=100,
            )

        assert ok is True
        kwargs = adapter._bot.edit_message_reply_markup.call_args.kwargs
        markup = kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 1
        assert [button.text for button in markup.inline_keyboard[0]] == [
            "Compress · ctx 50/100 (50%)",
            "ⓘ",
        ]

    @pytest.mark.asyncio
    async def test_ctxd_callback_looks_up_details_by_chat_and_message(self):
        adapter = _make_adapter()
        adapter._remember_context_badge_details(
            "12345",
            42,
            used=1234,
            total=5678,
            details={"profile": "gpt", "model": "openai/gpt-5.5"},
            session_key="agent:main:telegram:dm:12345:77",
        )

        query = AsyncMock()
        query.data = "ctxd:42"
        query.message = MagicMock(chat_id=12345)
        query.from_user = MagicMock(first_name="Norbert")
        query.answer = AsyncMock()
        update = MagicMock(callback_query=query)

        await adapter._handle_callback_query(update, MagicMock())

        query.answer.assert_awaited_once()
        kwargs = query.answer.await_args.kwargs
        assert kwargs["show_alert"] is True
        assert "Profile: gpt" in kwargs["text"]
        assert "Model: gpt-5.5" in kwargs["text"]
        assert "Context: 1.2k/5.7k (22%)" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_ctxd_callback_fails_closed_when_details_expired(self):
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "ctxd:42"
        query.message = MagicMock(chat_id=12345)
        query.from_user = MagicMock(first_name="Norbert")
        query.answer = AsyncMock()
        update = MagicMock(callback_query=query)

        await adapter._handle_callback_query(update, MagicMock())

        query.answer.assert_awaited_once_with(
            text="Session details expired; use a newer info button.",
            show_alert=True,
        )
