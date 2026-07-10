"""Focused adapter coverage for restored Discord live voice lifecycle."""

from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    DiscordVoiceReplyStreamer,
    VoiceReceiver,
)


class DummyAdapter:
    async def play_in_voice_channel(self, *args, **kwargs):
        return True


class DummyVoiceClient:
    channel = None
    user = None


def make_streamer(**kwargs):
    return DiscordVoiceReplyStreamer(DummyAdapter(), 123, **kwargs)


def make_adapter():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._voice_clients = {}
    adapter._voice_receivers = {}
    return adapter


def test_live_streamer_buffers_short_fragment_and_flushes_sentence():
    streamer = make_streamer(min_sentence_len=10)

    ready, remaining = streamer._extract_ready_chunks("Hi. This is ready.", flush_remainder=False)
    assert ready == []
    assert remaining == "Hi. This is ready."

    ready, remaining = streamer._extract_ready_chunks("First sentence. Second", flush_remainder=False)
    assert ready == ["First sentence."]
    assert remaining == "Second"


@pytest.mark.asyncio
async def test_live_streamer_drains_deltas_and_deduplicates_playback(monkeypatch):
    streamer = make_streamer(min_sentence_len=5)
    played = []

    async def fake_play(chunk):
        played.append(chunk)

    monkeypatch.setattr(streamer, "_play_chunk", fake_play)
    streamer.on_delta("Hello there.")
    streamer.on_delta("Hello there.")
    streamer.finish()
    await streamer.run()

    assert played == ["Hello there.", "Hello there."]
    assert streamer.ever_sent is False


def test_registering_new_live_stream_aborts_old_and_stops_playback():
    adapter = make_adapter()
    old = make_streamer()
    new = make_streamer()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = True
    adapter._voice_clients[123] = vc

    first = adapter.register_live_voice_streamer(123, old)
    second = adapter.register_live_voice_streamer(123, new)

    assert second == first + 1
    assert old._abort_event.is_set()
    vc.stop.assert_called_once()
    assert adapter._get_voice_turn_state(123)["streamer"] is new


@pytest.mark.asyncio
async def test_stale_generation_cannot_clear_or_barge_in_new_turn():
    adapter = make_adapter()
    streamer = make_streamer()
    generation = adapter.register_live_voice_streamer(123, streamer)
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = True
    adapter._voice_clients[123] = vc

    await adapter.clear_live_voice_streamer(123, generation=generation - 1)
    await adapter._handle_barge_in_event(123, {"generation": generation - 1})

    assert adapter._get_voice_turn_state(123)["streamer"] is streamer
    assert not streamer._abort_event.is_set()
    vc.stop.assert_not_called()


def test_authorized_barge_in_latches_once_and_unauthorized_audio_is_ignored():
    seen = []
    receiver = VoiceReceiver(
        DummyVoiceClient(),
        allowed_user_ids={"42"},
        barge_in_callback=seen.append,
        barge_in_config={"min_ms": 100, "min_pcm_ms": 100, "min_rms": 10},
    )
    receiver.enter_playback_monitor(7)
    pcm = (1000).to_bytes(2, "little", signed=True) * 9600

    assert receiver._observe_playback_monitor_pcm(111, pcm, now=1.0, user_id=99) is False
    assert receiver._observe_playback_monitor_pcm(111, pcm, now=1.1, user_id=42) is False
    assert receiver._observe_playback_monitor_pcm(111, pcm, now=1.25, user_id=42) is True
    assert receiver._observe_playback_monitor_pcm(111, pcm, now=1.4, user_id=42) is False
    assert seen and seen[0]["generation"] == 7 and seen[0]["user_id"] == 42


@pytest.mark.asyncio
async def test_busy_watchdog_stops_only_matching_generation(monkeypatch, tmp_path):
    adapter = make_adapter()
    generation = adapter.register_live_voice_streamer(123, make_streamer())
    asset = tmp_path / "busy.ogg"
    asset.write_bytes(b"audio")
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    adapter._voice_clients[123] = vc

    await adapter.start_busy_voice(123, asset_path=str(asset), generation=generation, watchdog_seconds=0.01)
    state = adapter._get_voice_turn_state(123)
    assert state["ambient_busy"] is True
    assert state["ambient_generation"] == generation

    await adapter.stop_busy_voice(123, generation=generation + 1)
    assert state["ambient_busy"] is True
    await adapter.stop_busy_voice(123, generation=generation)
    assert state["ambient_busy"] is False


def test_gateway_live_voice_wiring_selects_only_linked_discord_all_mode():
    from types import SimpleNamespace
    from gateway.run import GatewayRunner

    adapter = MagicMock()
    adapter._voice_text_channels = {123: 456}
    adapter.is_in_voice_channel.return_value = True
    from gateway.config import Platform
    source = SimpleNamespace(platform=Platform.DISCORD, chat_id="456")
    runner = object.__new__(GatewayRunner)
    runner.adapters = {source.platform: adapter}
    runner._voice_mode = {"discord:456": "all"}
    runner._voice_key = lambda platform, chat_id: f"{platform.value}:{chat_id}"

    assert runner._get_live_voice_reply_guild(source) == 123
    runner._voice_mode["discord:456"] = "voice_only"
    assert runner._get_live_voice_reply_guild(source) is None
