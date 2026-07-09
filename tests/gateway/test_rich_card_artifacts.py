import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.rich_cards.artifacts import (
    _CARD_FALLBACKS,
    _remember_card_media,
    find_card_artifacts,
    markdown_table_auto_enabled,
    render_rich_cards_in_response,
    response_to_ordered_segments,
)
from gateway.stream_consumer import GatewayStreamConsumer
from gateway.rich_cards.markdown_tables import _split_row
from gateway.rich_cards.renderer import cleanup_rich_card_cache
from gateway.rich_cards.validate import validate_and_repair


CARD = """Before

```message-card
kind: table
title: Build results
columns: [Check, Result]
rows:
  - [Unit tests, Pass]
```

After"""


class FakeAdapter(BasePlatformAdapter):
    def __init__(self, *, fail_images=False, raise_images=False):
        super().__init__(SimpleNamespace(extra={}), Platform.TELEGRAM)
        self.sent = []
        self.fail_images = fail_images
        self.raise_images = raise_images

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(("text", content, reply_to, metadata))
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
        if self.raise_images:
            raise RuntimeError("image exploded")
        self.sent.append(("image", Path(image_path).name, caption, metadata))
        return SendResult(success=not self.fail_images, message_id=f"m{len(self.sent)}", error="boom" if self.fail_images else None)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kwargs):
        self.sent.append(("document", Path(file_path).name, caption, metadata))
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def get_chat_info(self, chat_id):
        return {}


def test_message_card_artifact_renders_to_media_and_preserves_prose_order(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.rich_cards.renderer.get_hermes_home", lambda: tmp_path)
    rendered = render_rich_cards_in_response(CARD, platform="telegram", profile_home=tmp_path)
    assert "```message-card" not in rendered
    assert "Before" in rendered
    assert "After" in rendered
    assert "MEDIA:" in rendered
    segments = response_to_ordered_segments(rendered)
    assert [type(s).__name__ for s in segments] == ["TextSegment", "MediaSegment", "TextSegment"]


def test_invalid_artifact_uses_readable_fallback_not_raw_fence(tmp_path):
    text = "```message-card\nkind: chart\nchart:\n  type: bar\n```"
    rendered = render_rich_cards_in_response(text, platform="telegram", profile_home=tmp_path)
    assert "```message-card" not in rendered
    assert "MEDIA:" not in rendered
    assert "chart card unavailable" in rendered


def test_repair_does_not_drop_extra_table_cells():
    spec, repairs, error, _example = validate_and_repair({"kind": "table", "columns": ["A", "B"], "rows": [[1, 2, 3]]})
    assert error is None
    assert spec is not None
    assert spec.columns == ["A", "B", "Extra 1"]
    assert spec.rows == [[1, 2, 3]]
    assert any("extra columns" in repair for repair in repairs)


def test_markdown_table_split_handles_escaped_and_code_pipes():
    assert _split_row("| A | x \\| y | `a | b` |") == ["A", "x | y", "`a | b`"]


def test_ordered_rich_delivery_sends_text_card_text(tmp_path):
    png = tmp_path / "card.png"
    from PIL import Image
    Image.new("RGB", (20, 20), "red").save(png)
    adapter = FakeAdapter()
    rendered = f"Before\n\nMEDIA:{png}\n\nAfter"
    ok = asyncio.run(adapter._send_rendered_rich_response_ordered(chat_id="c", rendered_response=rendered, metadata={"thread_id": "t"}))
    assert ok is True
    assert [entry[0] for entry in adapter.sent] == ["text", "image", "text"]
    assert adapter.sent[0][1] == "Before"
    assert adapter.sent[2][1] == "After"


def test_ordered_delivery_handles_quoted_media_paths_and_fallback_on_send_failure(tmp_path):
    png = tmp_path / "card with space.png"
    from PIL import Image
    Image.new("RGB", (20, 20), "red").save(png)
    _CARD_FALLBACKS[str(png)] = "fallback table"
    adapter = FakeAdapter(fail_images=True)
    rendered = f"Before\nMEDIA:\"{png}\"\nAfter"
    ok = asyncio.run(adapter._send_rendered_rich_response_ordered(chat_id="c", rendered_response=rendered, metadata={}))
    assert ok is True
    assert [entry[0] for entry in adapter.sent] == ["text", "image", "text", "text"]
    assert adapter.sent[2][1] == "fallback table"
    assert adapter.sent[3][1] == "After"


def test_ordered_delivery_threads_alt_text_and_force_document(tmp_path):
    png = tmp_path / "card.png"
    from PIL import Image
    Image.new("RGB", (20, 20), "red").save(png)
    _remember_card_media(str(png), fallback_markdown="fallback", alt="Alt caption", force_document=True)
    segments = response_to_ordered_segments(f"MEDIA:{png}")
    assert type(segments[0]).__name__ == "MediaSegment"
    assert getattr(segments[0], "alt") == "Alt caption"
    assert getattr(segments[0], "force_document") is True
    adapter = FakeAdapter()
    ok = asyncio.run(adapter._send_rendered_rich_response_ordered(chat_id="c", rendered_response=f"MEDIA:{png}", metadata={}))
    assert ok is True
    assert adapter.sent == [("document", "card.png", "Alt caption", {})]


class ReplyRecordingAdapter(FakeAdapter):
    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
        self.sent.append(("image", Path(image_path).name, caption, reply_to, metadata))
        return SendResult(success=True, message_id=f"m{len(self.sent)}")


class FailingReplyRecordingAdapter(FakeAdapter):
    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
        self.sent.append(("image", Path(image_path).name, caption, reply_to, metadata))
        return SendResult(success=False, message_id=f"m{len(self.sent)}", error="boom")


def test_ordered_delivery_replies_to_card_when_media_is_first(tmp_path):
    png = tmp_path / "card.png"
    from PIL import Image
    Image.new("RGB", (20, 20), "red").save(png)
    adapter = ReplyRecordingAdapter()
    ok = asyncio.run(adapter._send_rendered_rich_response_ordered(chat_id="c", rendered_response=f"MEDIA:{png}\nAfter", reply_to="reply-1", metadata={}))
    assert ok is True
    assert adapter.sent[0] == ("image", "card.png", None, "reply-1", {})
    assert adapter.sent[1][0] == "text"


def test_ordered_delivery_replies_to_fallback_when_first_media_fails(tmp_path):
    png = tmp_path / "card.png"
    from PIL import Image
    Image.new("RGB", (20, 20), "red").save(png)
    _remember_card_media(str(png), fallback_markdown="fallback", alt="Alt", force_document=False)
    adapter = FailingReplyRecordingAdapter()
    ok = asyncio.run(adapter._send_rendered_rich_response_ordered(chat_id="c", rendered_response=f"MEDIA:{png}", reply_to="reply-1", metadata={}))
    assert ok is True
    assert adapter.sent == [
        ("image", "card.png", "Alt", "reply-1", {}),
        ("text", "fallback", "reply-1", {"notify": True}),
    ]


def test_ordered_delivery_counts_unsafe_media_fallback_as_delivered(tmp_path):
    unsafe = Path("/tmp/not-under-safe-root.png")
    _remember_card_media(str(unsafe), fallback_markdown="fallback", alt="Alt", force_document=False)
    adapter = FakeAdapter()
    ok = asyncio.run(adapter._send_rendered_rich_response_ordered(chat_id="c", rendered_response=f"MEDIA:{unsafe}", metadata={}))
    assert ok is True
    assert adapter.sent == [("text", "fallback", None, {"notify": True})]

def test_ordered_delivery_catches_media_exception_without_duplicate_text(tmp_path):
    png = tmp_path / "card.png"
    from PIL import Image
    Image.new("RGB", (20, 20), "red").save(png)
    _remember_card_media(str(png), fallback_markdown="fallback", alt="Alt", force_document=False)
    adapter = FakeAdapter(raise_images=True)
    ok = asyncio.run(adapter._send_rendered_rich_response_ordered(chat_id="c", rendered_response=f"Before\nMEDIA:{png}\nAfter", metadata={}))
    assert ok is True
    assert adapter.sent == [
        ("text", "Before", None, {"notify": True}),
        ("text", "fallback", None, {"notify": True}),
        ("text", "After", None, {"notify": True}),
    ]


def test_ordered_segments_strip_delivery_directives_from_text(tmp_path):
    png = tmp_path / "card.png"
    from PIL import Image
    Image.new("RGB", (20, 20), "red").save(png)
    segments = response_to_ordered_segments(f"[[as_document]]\nBefore\nMEDIA:{png}\n[[audio_as_voice]]\nAfter")
    assert [type(s).__name__ for s in segments] == ["TextSegment", "MediaSegment", "TextSegment"]
    assert getattr(segments[0], "markdown") == "Before"
    assert getattr(segments[2], "markdown") == "After"


def test_markdown_table_auto_remembers_fallback_for_media_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.rich_cards.renderer.get_hermes_home", lambda: tmp_path)
    source = "| A | B | C | D |\n| --- | --- | --- | --- |\n| 1 | 2 | 3 | 4 |\n| 5 | 6 | 7 | 8 |"
    rendered = render_rich_cards_in_response(source, platform="telegram", profile_home=tmp_path, markdown_table_auto=True)
    segments = response_to_ordered_segments(rendered)
    assert type(segments[0]).__name__ == "MediaSegment"
    assert getattr(segments[0], "fallback_markdown") == source


def test_markdown_table_auto_is_env_gated(monkeypatch):
    monkeypatch.delenv("HERMES_RICH_CARD_TABLE_AUTO", raising=False)
    assert markdown_table_auto_enabled("telegram") is False
    monkeypatch.setenv("HERMES_RICH_CARD_TABLE_AUTO", "1")
    monkeypatch.setenv("HERMES_RICH_CARD_TABLE_AUTO_PLATFORMS", "telegram")
    assert markdown_table_auto_enabled("telegram") is True
    assert markdown_table_auto_enabled("discord") is False


def test_receipt_card_validates_and_renders(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.rich_cards.renderer.get_hermes_home", lambda: tmp_path)
    text = """```message-card
kind: receipt
title: Test receipt
items:
  - label: Coffee
    qty: 2
    amount: $8.00
data:
  total: $8.00
```"""
    rendered = render_rich_cards_in_response(text, platform="telegram", profile_home=tmp_path)
    assert "MEDIA:" in rendered
    assert "```message-card" not in rendered
    segments = response_to_ordered_segments(rendered)
    assert type(segments[0]).__name__ == "MediaSegment"
    assert getattr(segments[0], "alt") == "Test receipt receipt card with 1 items"


def test_streaming_display_hides_message_card_fence_and_defers_final_delivery():
    text = "Before\n```message-card\nkind: table\ncolumns: [A, B]\nrows:\n - [1, 2]\n```\nAfter"
    cleaned = GatewayStreamConsumer._clean_for_display(text)
    assert "message-card" not in cleaned
    assert "kind: table" not in cleaned
    assert "Before" in cleaned and "After" in cleaned
    partial = GatewayStreamConsumer._clean_for_display("Before\n```mess")
    assert partial.strip() == "Before"
    assert GatewayStreamConsumer._has_rich_card_fence_candidate(text) is True
    assert GatewayStreamConsumer._has_rich_card_fence_candidate("Before\n```mess") is True
    assert GatewayStreamConsumer._has_rich_card_fence_candidate("Before\n```c") is False


def test_streaming_defers_markdown_table_when_auto_enabled(monkeypatch):
    table = "Before\n| A | B | C | D |\n| --- | --- | --- | --- |\n| 1 | 2 | 3 | 4 |\nAfter"
    monkeypatch.setenv("HERMES_RICH_CARD_TABLE_AUTO", "1")
    assert GatewayStreamConsumer._has_rich_card_fence_candidate(table, platform="telegram") is True
    monkeypatch.setenv("HERMES_RICH_CARD_TABLE_AUTO_PLATFORMS", "telegram")
    assert GatewayStreamConsumer._has_rich_card_fence_candidate(table, platform="discord") is False


def test_cleanup_rich_card_cache_removes_old_files_and_empty_dirs(tmp_path):
    cache_dir = tmp_path / "cache" / "rich_cards" / "abc"
    cache_dir.mkdir(parents=True)
    old_file = cache_dir / "card_old.png"
    fresh_file = cache_dir / "card_fresh.png"
    old_file.write_bytes(b"old")
    fresh_file.write_bytes(b"fresh")
    old_ts = 1_700_000_000
    os.utime(old_file, (old_ts, old_ts))
    removed = cleanup_rich_card_cache(max_age_hours=1, profile_home=tmp_path)
    assert removed == 1
    assert not old_file.exists()
    assert fresh_file.exists()
