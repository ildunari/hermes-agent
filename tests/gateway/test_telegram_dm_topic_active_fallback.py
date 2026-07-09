"""Tests for Telegram DM-topic active-lane fallback."""

from types import SimpleNamespace
from datetime import datetime

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
from gateway.platforms.telegram import TelegramAdapter
from telegram.constants import ChatType


def _adapter() -> TelegramAdapter:
    config = PlatformConfig(enabled=True, token="test-token")
    adapter = TelegramAdapter(config)
    adapter._dm_topics = {}
    adapter._last_dm_topic_by_chat = {}
    adapter._dm_topics_config = []
    return adapter


def _message(*, chat_id=5320274083, text="hello", message_thread_id=None, dm_topic_id=None):
    dm_topic = SimpleNamespace(topic_id=dm_topic_id) if dm_topic_id is not None else None
    chat = SimpleNamespace(
        id=chat_id,
        type=ChatType.PRIVATE,
        title=None,
        full_name="Kosta",
        is_forum=False,
    )
    user = SimpleNamespace(id=chat_id, full_name="Kosta")
    return SimpleNamespace(
        chat=chat,
        from_user=user,
        text=text,
        caption=None,
        message_id=123,
        message_thread_id=message_thread_id,
        direct_messages_topic=dm_topic,
        forum_topic_created=None,
        reply_to_message=None,
        date=None,
    )


def test_dm_topic_object_becomes_active_and_routes_event_to_topic():
    adapter = _adapter()

    event = adapter._build_message_event(
        _message(dm_topic_id=9925),
        MessageType.TEXT,
        update_id=1,
    )

    assert event.source.thread_id == "9925"
    assert adapter._active_dm_topic_for_chat("5320274083") == 9925


def test_unthreaded_private_update_uses_active_dm_topic_fallback():
    adapter = _adapter()
    adapter._remember_active_dm_topic("5320274083", 9925)

    event = adapter._build_message_event(
        _message(text="follow-up without Telegram topic fields"),
        MessageType.TEXT,
        update_id=2,
    )

    assert event.source.thread_id == "9925"


def test_general_topic_is_not_remembered_as_active_dm_topic():
    adapter = _adapter()
    adapter._remember_active_dm_topic("5320274083", 1)

    event = adapter._build_message_event(
        _message(text="root lane"),
        MessageType.TEXT,
        update_id=3,
    )

    assert adapter._active_dm_topic_for_chat("5320274083") is None
    assert event.source.thread_id is None


def test_unthreaded_dm_session_key_is_not_recovered_as_topic():
    adapter = _adapter()
    adapter._session_store = SimpleNamespace(
        _entries={
            "agent:main:telegram:dm:5320274083": SimpleNamespace(
                origin=SimpleNamespace(
                    chat_id="5320274083",
                    chat_type="dm",
                    thread_id=None,
                ),
                updated_at=datetime(2026, 5, 20, 21, 58, 58),
            )
        }
    )

    assert adapter._active_dm_topic_for_chat("5320274083") is None


def test_threaded_dm_session_key_can_still_be_recovered_as_topic():
    adapter = _adapter()
    adapter._session_store = SimpleNamespace(
        _entries={
            "agent:main:telegram:dm:5320274083:9925": SimpleNamespace(
                origin=SimpleNamespace(
                    chat_id="5320274083",
                    chat_type="dm",
                    thread_id=None,
                ),
                updated_at=datetime(2026, 5, 20, 21, 58, 58),
            )
        }
    )

    assert adapter._active_dm_topic_for_chat("5320274083") == 9925
