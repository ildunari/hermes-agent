"""KEEP BlueBubbles standalone delivery remains registry-first after main land."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platform_registry import PlatformEntry, platform_registry
from tools.send_message_tool import _send_to_platform


@pytest.mark.asyncio
async def test_bluebubbles_media_only_send_uses_registered_plugin(tmp_path):
    calls = []
    prior = platform_registry.get("bluebubbles")
    if prior is not None:
        platform_registry.unregister("bluebubbles")

    async def _send(
        pconfig,
        chat_id,
        message,
        *,
        thread_id=None,
        media_files=None,
        force_document=False,
    ):
        calls.append((chat_id, message, list(media_files or [])))
        return {"success": True, "message_id": "bb-test"}

    platform_registry.register(
        PlatformEntry(
            name="bluebubbles",
            label="BlueBubbles",
            adapter_factory=lambda config: None,
            check_fn=lambda: True,
            standalone_sender_fn=_send,
        )
    )
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"jpg")
    try:
        result = await _send_to_platform(
            Platform.BLUEBUBBLES,
            SimpleNamespace(extra={}),
            "+15555550123",
            "",
            media_files=[(str(media), False)],
        )
    finally:
        platform_registry.unregister("bluebubbles")
        if prior is not None:
            platform_registry.register(prior)

    assert result["success"] is True
    assert calls == [("+15555550123", "", [(str(media), False)])]
