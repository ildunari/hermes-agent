"""Focused tests for gateway-only slash command handlers."""

from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u1",
            chat_id="5320274083",
            user_name="tester",
            chat_type="dm",
            thread_id="12790",
        ),
        message_id="m1",
    )


@pytest.mark.asyncio
async def test_tts_command_requires_prompt():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)

    result = await runner._handle_tts_command(_event("/tts"))

    assert result == "Usage: /tts <prompt>"


@pytest.mark.asyncio
async def test_tts_command_sends_voice_reply():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._send_voice_reply = AsyncMock()
    event = _event("/tts say this out loud")

    result = await runner._handle_tts_command(event)

    assert result == "Sent voice message."
    runner._send_voice_reply.assert_awaited_once_with(event, "say this out loud")


@pytest.mark.asyncio
async def test_detached_restart_gateways_command_queues_helper_with_origin():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    event = _event("/restart_gateways --dry-run")

    with patch(
        "hermes_cli.restart_surfaces.enqueue_detached_restart",
        return_value="planned restart",
    ) as enqueue:
        result = await runner._handle_detached_surface_restart_command(
            event,
            "restart-gateways",
        )

    assert result == "planned restart"
    enqueue.assert_called_once_with(
        "gateways",
        delay=1.0,
        dry_run=True,
        notify_origin={
            "platform": "telegram",
            "chat_id": "5320274083",
            "thread_id": "12790",
        },
    )


@pytest.mark.asyncio
async def test_detached_restart_hermes_command_queues_helper():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    event = _event("/restart_hermes")

    with patch(
        "hermes_cli.restart_surfaces.enqueue_detached_restart",
        return_value="queued restart",
    ) as enqueue:
        result = await runner._handle_detached_surface_restart_command(
            event,
            "restart-hermes",
        )

    assert result == "queued restart"
    assert enqueue.call_args.args == ("hermes",)
    assert enqueue.call_args.kwargs["dry_run"] is False


@pytest.mark.asyncio
async def test_detached_restart_webui_command_queues_helper():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    event = _event("/restart_webui")

    with patch(
        "hermes_cli.restart_surfaces.enqueue_detached_restart",
        return_value="queued webui restart",
    ) as enqueue:
        result = await runner._handle_detached_surface_restart_command(
            event,
            "restart-webui",
        )

    assert result == "queued webui restart"
    assert enqueue.call_args.args == ("webui",)
    assert enqueue.call_args.kwargs["dry_run"] is False


@pytest.mark.asyncio
async def test_detached_restart_webui_refused_from_webui_surface():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    event = MessageEvent(
        text="/restart_webui",
        source=SessionSource(
            platform=Platform.API_SERVER,
            user_id="u1",
            chat_id="webui-chat",
            user_name="tester",
            chat_type="dm",
        ),
        message_id="m2",
    )

    with patch(
        "hermes_cli.restart_surfaces.enqueue_detached_restart",
    ) as enqueue:
        result = await runner._handle_detached_surface_restart_command(
            event,
            "restart-webui",
        )

    enqueue.assert_not_called()
    assert "Refusing" in result and "Telegram" in result
