"""Tests for gateway thread metadata propagation."""

from types import SimpleNamespace

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


def _make_runner():
    return object.__new__(gateway_run.GatewayRunner)


def test_thread_metadata_includes_chat_type_for_telegram_dm_topics():
    runner = _make_runner()
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5320274083",
        chat_type="dm",
        thread_id="9925",
    )

    assert runner._thread_metadata_for_source(source) == {
        "thread_id": "9925",
        "chat_type": "dm",
    }


def test_thread_metadata_omits_missing_thread():
    runner = _make_runner()
    source = SimpleNamespace(thread_id=None, chat_type="dm")

    assert runner._thread_metadata_for_source(source) is None
