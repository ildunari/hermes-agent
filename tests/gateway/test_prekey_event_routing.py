import asyncio
import dataclasses

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
from gateway.session import SessionSource


class _Adapter(BasePlatformAdapter):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, *args, **kwargs):
        return None

    async def send_message(self, *args, **kwargs):
        return None

    async def get_chat_info(self, *args, **kwargs):
        return None

    async def start_listening(self):
        return None


def _event(text):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.BLUEBUBBLES,
            chat_id="owner@example.com",
            chat_type="dm",
            user_id="owner@example.com",
        ),
    )


@pytest.mark.asyncio
async def test_prekey_route_keeps_first_turn_and_busy_followup_on_same_profile_lane():
    adapter = _Adapter(PlatformConfig(enabled=True, extra={}), Platform.BLUEBUBBLES)
    started = []
    busy = []

    async def prepare(event):
        event = dataclasses.replace(event)
        event.source = dataclasses.replace(event.source, profile="poke")
        return event

    async def handle_busy(event, session_key):
        busy.append((event.text, session_key))
        return True

    adapter.set_event_prepare_handler(prepare)
    adapter.set_busy_session_handler(handle_busy)
    adapter.set_message_handler(lambda _event: None)
    adapter._start_session_processing = lambda event, key: started.append((event.text, key)) or True

    await adapter.handle_message(_event("first"))
    session_key = started[0][1]
    assert session_key.startswith("agent:poke:bluebubbles:dm:")

    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._session_tasks[session_key] = asyncio.create_task(asyncio.sleep(60))
    try:
        await adapter.handle_message(_event("second"))
    finally:
        adapter._session_tasks[session_key].cancel()

    assert busy == [("second", session_key)]
