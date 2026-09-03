"""Registry-first standalone send routing for plugin-owned platforms.

Thin local glue (UPSTREAM-PR candidate): consult ``platform_registry`` for a
``standalone_sender_fn`` before built-in platform branches in
``send_message_tool._send_to_platform``.
"""

from __future__ import annotations


def platform_name(platform) -> str:
    return platform.value if hasattr(platform, "value") else str(platform)


def platform_has_standalone_sender(platform) -> bool:
    from gateway.platform_registry import platform_registry

    entry = platform_registry.get(platform_name(platform))
    return entry is not None and entry.standalone_sender_fn is not None


def platform_supports_registry_media(platform) -> bool:
    name = platform_name(platform)
    if name == "buzz":
        return True
    return platform_has_standalone_sender(platform)


async def try_send_via_registry(
    platform,
    pconfig,
    chat_id,
    chunk,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Send via ``_send_via_adapter`` when the registry exposes ``standalone_sender_fn``.

    Returns ``None`` when no registry sender is registered (caller may fall back).
    """
    if not platform_has_standalone_sender(platform):
        return None
    from tools.send_message_tool import _send_via_adapter

    return await _send_via_adapter(
        platform,
        pconfig,
        chat_id,
        chunk,
        thread_id=thread_id,
        media_files=media_files,
        force_document=force_document,
    )
