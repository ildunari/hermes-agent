"""Tests for the BlueBubbles iMessage gateway adapter."""
import asyncio
import json

import pytest

from gateway.config import Platform, PlatformConfig


def _make_adapter(monkeypatch, **extra):
    monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://localhost:1234")
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
    monkeypatch.delenv("BLUEBUBBLES_WEBHOOK_PUBLIC_URL", raising=False)
    monkeypatch.delenv("BLUEBUBBLES_WEBHOOK_URL", raising=False)
    from gateway.platforms.bluebubbles import BlueBubblesAdapter

    cfg = PlatformConfig(
        enabled=True,
        extra={
            "server_url": "http://localhost:1234",
            "password": "secret",
            "text_batch_delay_seconds": 0,
            **extra,
        },
    )
    return BlueBubblesAdapter(cfg)


class TestBlueBubblesStatusFiltering:
    def test_lifecycle_statuses_are_not_sent_as_imessage_bubbles(self):
        from gateway.run import _prepare_gateway_status_message

        leaked = (
            "🔀 Model auto-switched: claude-opus-4-8 → gpt-5.5 "
            "(provider: openai-codex, reason: timeout). The selected model failed; "
            "continuing on the fallback."
        )

        assert _prepare_gateway_status_message(
            Platform.BLUEBUBBLES,
            "lifecycle",
            leaked,
        ) is None

    def test_non_lifecycle_warnings_still_reach_bluebubbles(self):
        from gateway.run import _prepare_gateway_status_message

        assert _prepare_gateway_status_message(
            Platform.BLUEBUBBLES,
            "warn",
            "Memory flush failed; check logs.",
        ) == "Memory flush failed; check logs."


class TestBlueBubblesConfigLoading:
    def test_apply_env_overrides_bluebubbles(self, monkeypatch):
        monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://localhost:1234")
        monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
        monkeypatch.setenv("BLUEBUBBLES_WEBHOOK_PORT", "9999")
        monkeypatch.setenv("BLUEBUBBLES_REQUIRE_MENTION", "true")
        monkeypatch.setenv("BLUEBUBBLES_MENTION_PATTERNS", r'["(?i)^amos\\b"]')
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform.BLUEBUBBLES in config.platforms
        bc = config.platforms[Platform.BLUEBUBBLES]
        assert bc.enabled is True
        assert bc.extra["server_url"] == "http://localhost:1234"
        assert bc.extra["password"] == "secret"
        assert bc.extra["webhook_port"] == 9999
        assert bc.extra["require_mention"] is True
        assert bc.extra["mention_patterns"] == ["(?i)^amos\\b"]

    def test_apply_env_sets_cross_host_webhook_public_url(self, monkeypatch):
        monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://mini:1234")
        monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
        monkeypatch.setenv("BLUEBUBBLES_WEBHOOK_HOST", "0.0.0.0")
        monkeypatch.setenv("BLUEBUBBLES_WEBHOOK_PUBLIC_URL", "http://100.64.0.1:8645/bluebubbles-webhook")
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        bc = config.platforms[Platform.BLUEBUBBLES]
        assert bc.extra["webhook_host"] == "0.0.0.0"
        assert bc.extra["webhook_public_url"] == "http://100.64.0.1:8645/bluebubbles-webhook"

    def test_home_channel_set_from_env(self, monkeypatch):
        monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://localhost:1234")
        monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
        monkeypatch.setenv("BLUEBUBBLES_HOME_CHANNEL", "user@example.com")
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        hc = config.platforms[Platform.BLUEBUBBLES].home_channel
        assert hc is not None
        assert hc.chat_id == "user@example.com"

    def test_not_connected_without_password(self, monkeypatch):
        monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://localhost:1234")
        monkeypatch.delenv("BLUEBUBBLES_PASSWORD", raising=False)
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform.BLUEBUBBLES not in config.get_connected_platforms()


class TestBlueBubblesHelpers:
    def test_check_requirements(self, monkeypatch):
        monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://localhost:1234")
        monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
        from gateway.platforms.bluebubbles import check_bluebubbles_requirements

        assert check_bluebubbles_requirements() is True

    def test_supports_message_editing_is_false(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter.SUPPORTS_MESSAGE_EDITING is False

    def test_truncate_message_omits_pagination_suffixes(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        chunks = adapter.truncate_message("abcdefghij", max_length=6)
        assert len(chunks) > 1
        assert "".join(chunks) == "abcdefghij"
        assert all("(" not in chunk for chunk in chunks)

    @pytest.mark.asyncio
    async def test_send_image_file_puts_caption_in_attachment_request_not_second_text(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch)
        image = tmp_path / "photo.png"
        image.write_bytes(b"fake-png")
        attachment_posts = []
        text_posts = []

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": 200, "data": {"guid": "msg-with-attachment"}}

        class FakeClient:
            async def post(self, url, files=None, data=None, json=None, **kwargs):
                if "/api/v1/message/attachment" in url:
                    attachment_posts.append(data)
                    return FakeResponse()
                if "/api/v1/message/text" in url:
                    text_posts.append(json)
                    return FakeResponse()
                raise AssertionError(url)

        adapter.client = FakeClient()  # type: ignore[assignment]
        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)

        result = await adapter.send_image_file("user@example.com", str(image), caption="caption text")

        assert result.success is True
        assert text_posts == []
        assert len(attachment_posts) == 1
        payload = attachment_posts[0]
        assert payload["chatGuid"] == "iMessage;-;user@example.com"
        assert payload["message"] == "caption text"
        assert payload["text"] == "caption text"
        assert payload["caption"] == "caption text"

    @pytest.mark.asyncio
    async def test_send_keeps_paragraphs_in_one_bubble_by_default(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        sent = []

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        class FakeResponse:
            def __init__(self, guid):
                self._guid = guid

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"guid": self._guid}}

        class FakeClient:
            async def post(self, url, json=None, **kwargs):
                assert json is not None
                sent.append(json["message"])
                return FakeResponse(f"msg-{len(sent)}")

        adapter.client = FakeClient()  # type: ignore[assignment]
        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)

        result = await adapter.send("user@example.com", "first thought\n\nsecond thought")

        assert result.success is True
        assert sent == ["first thought\n\nsecond thought"]

    @pytest.mark.asyncio
    async def test_send_can_opt_into_paragraph_splitting(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, split_outbound_paragraphs=True)
        sent = []

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        class FakeResponse:
            def __init__(self, guid):
                self._guid = guid

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"guid": self._guid}}

        class FakeClient:
            async def post(self, url, json=None, **kwargs):
                assert json is not None
                sent.append(json["message"])
                return FakeResponse(f"msg-{len(sent)}")

        adapter.client = FakeClient()  # type: ignore[assignment]
        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)

        result = await adapter.send("user@example.com", "first thought\n\nsecond thought")

        assert result.success is True
        assert sent == ["first thought", "second thought"]

    @pytest.mark.asyncio
    async def test_send_marks_late_chunk_failure_as_partial_delivery(self, monkeypatch):
        import httpx

        adapter = _make_adapter(monkeypatch, split_outbound_paragraphs=True)
        sent = []

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"guid": "first-msg"}}

        class FakeClient:
            async def post(self, url, json=None, **kwargs):
                assert json is not None
                sent.append(json["message"])
                if len(sent) == 2:
                    raise httpx.ConnectError("second chunk failed")
                return FakeResponse()

        adapter.client = FakeClient()  # type: ignore[assignment]
        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)

        result = await adapter.send("user@example.com", "first\n\nsecond")

        assert sent == ["first", "second"]
        assert result.success is False
        assert result.retryable is False
        assert result.raw_response == {
            "partial_delivery": True,
            "skip_plaintext_fallback": True,
        }

    @pytest.mark.asyncio
    async def test_send_marks_timeout_non_retryable_with_error_text(self, monkeypatch):
        import httpx

        adapter = _make_adapter(monkeypatch)

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        class FakeClient:
            async def post(self, url, json=None, **kwargs):
                raise httpx.ReadTimeout("slow BlueBubbles send")

        adapter.client = FakeClient()  # type: ignore[assignment]
        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)

        result = await adapter.send("user@example.com", "hello")

        assert result.success is False
        assert result.retryable is False
        assert "ReadTimeout" in (result.error or "")

    @pytest.mark.asyncio
    async def test_typing_refreshes_private_api_helper_status(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._private_api_enabled = True
        adapter._helper_connected = False
        calls = []

        class FakeClient:
            async def post(self, url, **kwargs):
                calls.append((url, kwargs))

        async def fake_api_get(path):
            assert path == "/api/v1/server/info"
            return {"data": {"private_api": True, "helper_connected": True}}

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        adapter.client = FakeClient()  # type: ignore[assignment]
        monkeypatch.setattr(adapter, "_api_get", fake_api_get)
        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)

        await adapter.send_typing("user@example.com")

        assert adapter._helper_connected is True
        assert calls
        assert "/api/v1/chat/iMessage%3B-%3Buser%40example.com/typing" in calls[0][0]

    @pytest.mark.asyncio
    async def test_mark_read_returns_false_when_helper_still_unavailable(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._private_api_enabled = True
        adapter._helper_connected = False

        class FakeClient:
            async def post(self, url, **kwargs):  # pragma: no cover - should not be called
                raise AssertionError("mark_read should not post when helper is unavailable")

        async def fake_api_get(path):
            return {"data": {"private_api": True, "helper_connected": False}}

        adapter.client = FakeClient()  # type: ignore[assignment]
        monkeypatch.setattr(adapter, "_api_get", fake_api_get)

        assert await adapter.mark_read("user@example.com") is False

    def test_format_message_strips_markdown(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter.format_message("**Hello** `world`") == "Hello world"

    def test_format_message_preserves_underscores_in_identifiers(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        text = "Use /api_v2 with FEATURE_FLAG_NAME and config_file.json"
        assert adapter.format_message(text) == text

    def test_strip_markdown_headers(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter.format_message("## Heading\ntext") == "Heading\ntext"

    def test_strip_markdown_links(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter.format_message("[click here](http://example.com)") == "click here"

    def test_webhook_register_can_be_disabled_for_external_router(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_register=False)
        assert adapter.webhook_register is False

    @pytest.mark.asyncio
    async def test_disconnect_does_not_unregister_when_external_router_owns_webhook(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_register=False)
        called = False

        async def fake_unregister():
            nonlocal called
            called = True

        monkeypatch.setattr(adapter, "_unregister_webhook", fake_unregister)
        await adapter.disconnect()
        assert called is False

    @pytest.mark.asyncio
    async def test_disconnect_does_not_unregister_without_successful_registration(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        called = False

        async def fake_unregister():
            nonlocal called
            called = True

        monkeypatch.setattr(adapter, "_unregister_webhook", fake_unregister)
        await adapter.disconnect()
        assert called is False

    @pytest.mark.asyncio
    async def test_disconnect_unregisters_after_successful_registration(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._registered_webhook = True
        called = False

        async def fake_unregister():
            nonlocal called
            called = True

        monkeypatch.setattr(adapter, "_unregister_webhook", fake_unregister)
        await adapter.disconnect()
        assert called is True
        assert adapter._registered_webhook is False

    @pytest.mark.asyncio
    async def test_connect_send_only_does_not_bind_webhook_or_register(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_register=False)

        async def fake_api_get(path):
            if path == "/api/v1/server/info":
                return {"data": {"private_api": True, "helper_connected": True}}
            return {"status": 200}

        async def fail_register():
            raise AssertionError("send-only mode must not register webhook")

        class FailTCPSite:
            def __init__(self, *args, **kwargs):
                raise AssertionError("send-only mode must not bind webhook port")

        monkeypatch.setattr(adapter, "_api_get", fake_api_get)
        monkeypatch.setattr(adapter, "_register_webhook", fail_register)
        import aiohttp.web
        monkeypatch.setattr(aiohttp.web, "TCPSite", FailTCPSite)

        assert await adapter.connect() is True
        assert adapter._runner is None
        assert adapter.is_connected is True
        await adapter.disconnect()

    def test_init_normalizes_webhook_path(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_path="bluebubbles-webhook")
        assert adapter.webhook_path == "/bluebubbles-webhook"

    def test_init_preserves_leading_slash(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_path="/my-hook")
        assert adapter.webhook_path == "/my-hook"

    def test_webhook_public_url_overrides_bind_host_for_cross_host_server(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            webhook_host="0.0.0.0",
            webhook_public_url="http://100.64.0.1:8645/bluebubbles-webhook",
        )
        assert adapter._webhook_url == "http://100.64.0.1:8645/bluebubbles-webhook"
        assert adapter._webhook_register_url == (
            "http://100.64.0.1:8645/bluebubbles-webhook?password=secret"
        )

    def test_server_url_normalized(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, server_url="http://localhost:1234/")
        assert adapter.server_url == "http://localhost:1234"

    def test_server_url_adds_scheme(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, server_url="localhost:1234")
        assert adapter.server_url == "http://localhost:1234"

    def test_default_mention_patterns_match_hermes_variants(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, require_mention=True)

        assert adapter.require_mention is True
        assert adapter._message_matches_mention_patterns("Hermes, summarize this")
        assert adapter._message_matches_mention_patterns("@Hermes agent help")
        assert not adapter._message_matches_mention_patterns("casual family chatter")
        assert not adapter._message_matches_mention_patterns("antihermes should not match")

    def test_custom_mention_patterns_override_defaults(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            require_mention=True,
            mention_patterns=[r"(?<![\w@])@?amos\b[,:\-]?"],
        )

        assert adapter._message_matches_mention_patterns("Amos what is next?")
        assert not adapter._message_matches_mention_patterns("Hermes what is next?")

    def test_clean_mention_text_strips_leading_wake_word(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, require_mention=True)

        assert adapter._clean_mention_text("Hermes, summarize this") == "summarize this"
        assert adapter._clean_mention_text("Hermes agent: summarize this") == "summarize this"
        assert adapter._clean_mention_text("please ask Hermes about this") == "please ask Hermes about this"


class _FakeBlueBubblesRequest:
    def __init__(self, payload, password="secret"):
        self.query = {"password": password}
        self.headers = {}
        self._body = json.dumps(payload).encode("utf-8")

    async def read(self):
        return self._body


class TestBlueBubblesMentionGating:
    @pytest.mark.asyncio
    async def test_group_message_without_mention_is_acknowledged_and_skipped(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            require_mention=True,
            send_read_receipts=False,
        )
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        response = await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "new-message",
            "data": {
                "guid": "msg-1",
                "text": "casual family chatter",
                "handle": {"address": "+15555550100"},
                "isFromMe": False,
                "isGroup": True,
                "chats": [{"guid": "iMessage;+;group-chat"}],
            },
        }))
        await asyncio.sleep(0)

        assert response.status == 200
        assert handled == []

    @pytest.mark.asyncio
    async def test_group_reply_without_mention_to_non_hermes_message_is_observed_only(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            require_mention=True,
            send_read_receipts=False,
        )
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        adapter._message_handler = fake_handle_message
        response = await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "new-message",
            "data": {
                "guid": "msg-reply-other",
                "text": "yeah that works",
                "associatedMessageGuid": "someone-else-message",
                "handle": {"address": "+155****0100"},
                "isFromMe": False,
                "isGroup": True,
                "chats": [{"guid": "iMessage;+;group-chat"}],
            },
        }))
        await asyncio.sleep(0)

        assert response.status == 200
        assert len(handled) == 1
        assert handled[0].observed_only is True
        assert handled[0].reply_to_message_id is None

    @pytest.mark.asyncio
    async def test_group_reply_without_mention_to_recent_hermes_message_is_addressed(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            require_mention=True,
            send_read_receipts=False,
        )
        adapter._remember_outbound_message_guid("hermes-message-1")
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        response = await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "new-message",
            "data": {
                "guid": "msg-reply-hermes",
                "text": "yes",
                "associatedMessageGuid": "hermes-message-1",
                "handle": {"address": "+155****0100"},
                "isFromMe": False,
                "isGroup": True,
                "chats": [{"guid": "iMessage;+;group-chat"}],
            },
        }))
        await asyncio.sleep(0)

        assert response.status == 200
        assert len(handled) == 1
        assert handled[0].observed_only is False
        assert handled[0].reply_to_message_id == "hermes-message-1"

    @pytest.mark.asyncio
    async def test_group_message_with_default_mention_is_dispatched_cleaned(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            require_mention=True,
            send_read_receipts=False,
        )
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        response = await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "new-message",
            "data": {
                "guid": "msg-2",
                "text": "Hermes, summarize this",
                "handle": {"address": "+15555550100"},
                "isFromMe": False,
                "isGroup": True,
                "chats": [{"guid": "iMessage;+;group-chat"}],
            },
        }))
        await asyncio.sleep(0)

        assert response.status == 200
        assert [event.text for event in handled] == ["summarize this"]

    @pytest.mark.asyncio
    async def test_dm_message_does_not_require_mention(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            require_mention=True,
            send_read_receipts=False,
        )
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        response = await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "new-message",
            "data": {
                "guid": "msg-3",
                "text": "hello from a dm",
                "handle": {"address": "user@example.com"},
                "isFromMe": False,
                "chatGuid": "iMessage;-;user@example.com",
                "chatIdentifier": "user@example.com",
            },
        }))
        await asyncio.sleep(0)

        assert response.status == 200
        assert [event.text for event in handled] == ["hello from a dm"]

class TestBlueBubblesTextBatching:
    @pytest.mark.asyncio
    async def test_dm_text_webhooks_are_batched_before_dispatch(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            text_batch_delay_seconds=0.03,
            text_batch_link_delay_seconds=0.03,
            send_read_receipts=False,
        )
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        base = {
            "type": "new-message",
            "data": {
                "handle": {"address": "user@example.com"},
                "isFromMe": False,
                "chatGuid": "iMessage;-;user@example.com",
                "chatIdentifier": "user@example.com",
            },
        }

        first = json.loads(json.dumps(base))
        first["data"].update({"guid": "msg-batch-1", "text": "https://instagram.com/reel/abc"})
        second = json.loads(json.dumps(base))
        second["data"].update({"guid": "msg-batch-2", "text": "Can you figure out if they ended up together?"})

        response1 = await adapter._handle_webhook(_FakeBlueBubblesRequest(first))
        await asyncio.sleep(0.01)
        response2 = await adapter._handle_webhook(_FakeBlueBubblesRequest(second))
        await asyncio.sleep(0.05)

        assert response1.status == 200
        assert response2.status == 200
        assert len(handled) == 1
        assert handled[0].text == "https://instagram.com/reel/abc\nCan you figure out if they ended up together?"
        assert handled[0].message_id == "msg-batch-2"

    @pytest.mark.asyncio
    async def test_media_then_text_webhooks_are_batched_into_one_agent_turn(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            text_batch_delay_seconds=0.03,
            text_batch_link_delay_seconds=0.03,
            send_read_receipts=False,
        )
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        async def fake_download_attachment(att_guid, att_meta):
            assert att_guid == "att-image-1"
            return "/tmp/bluebubbles-photo.png"

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        monkeypatch.setattr(adapter, "_download_attachment", fake_download_attachment)
        base = {
            "type": "new-message",
            "data": {
                "handle": {"address": "user@example.com"},
                "isFromMe": False,
                "chatGuid": "iMessage;-;user@example.com",
                "chatIdentifier": "user@example.com",
            },
        }
        image = json.loads(json.dumps(base))
        image["data"].update({
            "guid": "msg-image",
            "attachments": [{"guid": "att-image-1", "mimeType": "image/png", "transferName": "photo.png"}],
        })
        text = json.loads(json.dumps(base))
        text["data"].update({"guid": "msg-caption", "text": "What does this say?"})

        response1 = await adapter._handle_webhook(_FakeBlueBubblesRequest(image))
        await asyncio.sleep(0.01)
        response2 = await adapter._handle_webhook(_FakeBlueBubblesRequest(text))
        await asyncio.sleep(0.05)

        assert response1.status == 200
        assert response2.status == 200
        assert len(handled) == 1
        assert handled[0].text == "What does this say?"
        assert handled[0].media_urls == ["/tmp/bluebubbles-photo.png"]
        assert handled[0].media_types == ["image/png"]
        assert handled[0].message_id == "msg-caption"

    @pytest.mark.asyncio
    async def test_text_batching_can_be_disabled_for_immediate_dispatch(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            text_batch_delay_seconds=0,
            send_read_receipts=False,
        )
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        response = await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "new-message",
            "data": {
                "guid": "msg-immediate",
                "text": "hello from a dm",
                "handle": {"address": "user@example.com"},
                "isFromMe": False,
                "chatGuid": "iMessage;-;user@example.com",
                "chatIdentifier": "user@example.com",
            },
        }))
        await asyncio.sleep(0)

        assert response.status == 200
        assert [event.text for event in handled] == ["hello from a dm"]


class TestBlueBubblesWebhookParsing:
    def test_webhook_prefers_chat_guid_over_message_guid(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        payload = {
            "guid": "MESSAGE-GUID",
            "chatGuid": "iMessage;-;user@example.com",
            "chatIdentifier": "user@example.com",
        }
        record = adapter._extract_payload_record(payload) or {}
        chat_guid = adapter._value(
            record.get("chatGuid"),
            payload.get("chatGuid"),
            record.get("chat_guid"),
            payload.get("chat_guid"),
            payload.get("guid"),
        )
        assert chat_guid == "iMessage;-;user@example.com"

    def test_webhook_can_fall_back_to_sender_when_chat_fields_missing(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        payload = {
            "data": {
                "guid": "MESSAGE-GUID",
                "text": "hello",
                "handle": {"address": "user@example.com"},
                "isFromMe": False,
            }
        }
        record = adapter._extract_payload_record(payload) or {}
        chat_guid = adapter._value(
            record.get("chatGuid"),
            payload.get("chatGuid"),
            record.get("chat_guid"),
            payload.get("chat_guid"),
            payload.get("guid"),
        )
        chat_identifier = adapter._value(
            record.get("chatIdentifier"),
            record.get("identifier"),
            payload.get("chatIdentifier"),
            payload.get("identifier"),
        )
        sender = (
            adapter._value(
                record.get("handle", {}).get("address")
                if isinstance(record.get("handle"), dict)
                else None,
                record.get("sender"),
                record.get("from"),
                record.get("address"),
            )
            or chat_identifier
            or chat_guid
        )
        if not (chat_guid or chat_identifier) and sender:
            chat_identifier = sender
        assert chat_identifier == "user@example.com"

    def test_canonical_session_chat_id_uses_sender_for_dm_raw_guid(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter._canonical_session_chat_id(
            "any;-;+18135551212",
            None,
            "+18135551212",
            is_group=False,
        ) == "+18135551212"

    def test_canonical_session_chat_id_keeps_group_guid(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter._canonical_session_chat_id(
            "iMessage;+;chat-guid",
            None,
            "+18135551212",
            is_group=True,
        ) == "iMessage;+;chat-guid"

    def test_group_mention_gate_strips_prefix(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter.group_require_mention is True
        assert adapter._strip_group_mention("@Hermes summarize this") == "summarize this"
        assert adapter._strip_group_mention("Hermes: /status") == "/status"
        assert adapter._strip_group_mention("Hermes /status") == "/status"
        assert adapter._strip_group_mention("random group chatter") is None

    def test_group_mention_gate_can_be_disabled(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, group_require_mention=False)
        assert adapter.group_require_mention is False

    def test_bluebubbles_group_session_is_shared_across_senders(self, monkeypatch):
        from gateway.session import SessionSource, build_session_key, is_shared_multi_user_session

        source_a = SessionSource(
            platform=Platform.BLUEBUBBLES,
            chat_id="iMessage;+;group-guid",
            chat_type="group",
            user_id="+155****0001",
        )
        source_b = SessionSource(
            platform=Platform.BLUEBUBBLES,
            chat_id="iMessage;+;group-guid",
            chat_type="group",
            user_id="+155****0002",
        )

        assert build_session_key(source_a) == build_session_key(source_b)
        assert is_shared_multi_user_session(source_a) is True

    def test_webhook_canonicalizes_dm_session_to_sender(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        payload = {
            "data": {
                "guid": "MESSAGE-GUID",
                "text": "hello",
                "chats": [{"guid": "any;-;+18135551212"}],
                "handle": {"address": "+18135551212"},
                "isFromMe": False,
            }
        }
        record = adapter._extract_payload_record(payload) or {}
        chat_guid = record.get("chats", [{}])[0].get("guid")
        sender = record.get("handle", {}).get("address")
        assert adapter._canonical_session_chat_id(
            chat_guid,
            None,
            sender,
            is_group=False,
        ) == "+18135551212"

    def test_dm_contact_identity_canonicalizes_email_case(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter._canonical_session_chat_id(
            "iMessage;-;KOSTA@EXAMPLE.COM",
            " KOSTA@EXAMPLE.COM ",
            "kosta@example.com",
            is_group=False,
        ) == "kosta@example.com"

    def test_dm_contact_identity_prefers_guid_phone_over_display_format(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter._canonical_session_chat_id(
            "iMessage;-;+18135551212",
            None,
            "(813) 555-1212",
            is_group=False,
        ) == "+18135551212"

    def test_webhook_extracts_chat_guid_from_chats_array_dm(self, monkeypatch):
        """BB v1.9+ webhook payloads omit top-level chatGuid; GUID is in chats[0].guid."""
        adapter = _make_adapter(monkeypatch)
        payload = {
            "type": "new-message",
            "data": {
                "guid": "MESSAGE-GUID",
                "text": "hello",
                "handle": {"address": "+15551234567"},
                "isFromMe": False,
                "chats": [
                    {"guid": "any;-;+15551234567", "chatIdentifier": "+15551234567"}
                ],
            },
        }
        record = adapter._extract_payload_record(payload) or {}
        chat_guid = adapter._value(
            record.get("chatGuid"),
            payload.get("chatGuid"),
            record.get("chat_guid"),
            payload.get("chat_guid"),
            payload.get("guid"),
        )
        if not chat_guid:
            _chats = record.get("chats") or []
            if _chats and isinstance(_chats[0], dict):
                chat_guid = _chats[0].get("guid") or _chats[0].get("chatGuid")
        assert chat_guid == "any;-;+15551234567"

    def test_webhook_extracts_chat_guid_from_chats_array_group(self, monkeypatch):
        """Group chat GUIDs contain ;+; and must be extracted from chats array."""
        adapter = _make_adapter(monkeypatch)
        payload = {
            "type": "new-message",
            "data": {
                "guid": "MESSAGE-GUID",
                "text": "hello everyone",
                "handle": {"address": "+15551234567"},
                "isFromMe": False,
                "isGroup": True,
                "chats": [{"guid": "any;+;chat-uuid-abc123"}],
            },
        }
        record = adapter._extract_payload_record(payload) or {}
        chat_guid = adapter._value(
            record.get("chatGuid"),
            payload.get("chatGuid"),
            record.get("chat_guid"),
            payload.get("chat_guid"),
            payload.get("guid"),
        )
        if not chat_guid:
            _chats = record.get("chats") or []
            if _chats and isinstance(_chats[0], dict):
                chat_guid = _chats[0].get("guid") or _chats[0].get("chatGuid")
        assert chat_guid == "any;+;chat-uuid-abc123"

    def test_extract_payload_record_accepts_list_data(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        payload = {
            "type": "new-message",
            "data": [
                {
                    "text": "hello",
                    "chatGuid": "iMessage;-;user@example.com",
                    "chatIdentifier": "user@example.com",
                }
            ],
        }
        record = adapter._extract_payload_record(payload)
        assert record == payload["data"][0]

    def test_extract_payload_record_dict_data(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        payload = {"data": {"text": "hello", "chatGuid": "iMessage;-;+1234"}}
        record = adapter._extract_payload_record(payload)
        assert record["text"] == "hello"

    def test_extract_payload_record_fallback_to_message(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        payload = {"message": {"text": "hello"}}
        record = adapter._extract_payload_record(payload)
        assert record["text"] == "hello"

    @pytest.mark.asyncio
    async def test_webhook_deduplicates_message_guid(self, monkeypatch):
        import asyncio
        import json

        adapter = _make_adapter(monkeypatch)
        seen = []

        async def collect(event):
            seen.append(event)

        async def fake_handle_message(event):
            await collect(event)

        class FakeRequest:
            query = {"password": "secret"}
            headers = {}

            async def read(self):
                return json.dumps({
                    "type": "new-message",
                    "data": {
                        "guid": "message-guid-1",
                        "text": "hello",
                        "chatGuid": "iMessage;-;+18135551212",
                        "handle": {"address": "+18135551212"},
                        "isFromMe": False,
                    },
                }).encode()

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

        await adapter._handle_webhook(FakeRequest())
        await adapter._handle_webhook(FakeRequest())
        if adapter._background_tasks:
            await asyncio.gather(*adapter._background_tasks)

        assert len(seen) == 1
    @pytest.mark.asyncio
    async def test_webhook_dedupe_cache_is_bounded(self, monkeypatch):
        import asyncio
        import json

        adapter = _make_adapter(monkeypatch, message_dedupe_limit=1)
        seen = []

        async def fake_handle_message(event):
            seen.append(event.message_id)

        class FakeRequest:
            query = {"password": "secret"}
            headers = {}
            counter = 0

            async def read(self):
                self.__class__.counter += 1
                guid = f"message-guid-{self.__class__.counter}"
                return json.dumps({
                    "type": "new-message",
                    "data": {
                        "guid": guid,
                        "text": "hello",
                        "chatGuid": "iMessage;-;+18135551212",
                        "handle": {"address": "+18135551212"},
                        "isFromMe": False,
                    },
                }).encode()

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        await adapter._handle_webhook(FakeRequest())
        await adapter._handle_webhook(FakeRequest())
        if adapter._background_tasks:
            await asyncio.gather(*adapter._background_tasks)

        assert len(seen) == 2
        assert len(adapter._seen_message_guids) == 1


class TestBlueBubblesGuidResolution:
    def test_raw_guid_returned_as_is(self, monkeypatch):
        """If target already contains ';' it's a raw GUID — return unchanged."""
        adapter = _make_adapter(monkeypatch)
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            adapter._resolve_chat_guid("iMessage;-;user@example.com")
        )
        assert result == "iMessage;-;user@example.com"

    def test_empty_target_returns_none(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            adapter._resolve_chat_guid("")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_exact_chat_identifier_match_returns_dm_guid(self, monkeypatch):
        """A 1:1 DM whose chatIdentifier equals the target resolves to its guid."""
        adapter = _make_adapter(monkeypatch)

        async def fake_api_post(path, payload):
            return {
                "data": [
                    {
                        "guid": "iMessage;-;user@example.com",
                        "chatIdentifier": "user@example.com",
                        "participants": [{"address": "user@example.com"}],
                    }
                ]
            }

        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        result = await adapter._resolve_chat_guid("user@example.com")
        assert result == "iMessage;-;user@example.com"

    @pytest.mark.asyncio
    async def test_participant_only_match_does_not_resolve_to_group(self, monkeypatch):
        """Regression for #24157: contact appearing as a participant in a group
        chat must NOT be selected when no DM with that exact chatIdentifier exists.

        Otherwise an outbound DM reply leaks into the group thread.
        """
        adapter = _make_adapter(monkeypatch)

        async def fake_api_post(path, payload):
            return {
                "data": [
                    {
                        "guid": "iMessage;+;chat0000000000-family-group",
                        "chatIdentifier": "chat0000000000",
                        "participants": [
                            {"address": "user@example.com"},
                            {"address": "+15555550100"},
                        ],
                    }
                ]
            }

        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        result = await adapter._resolve_chat_guid("user@example.com")
        assert result is None, (
            "participant-only match must not resolve to a group GUID — DM "
            "replies would leak into the group thread"
        )

    @pytest.mark.asyncio
    async def test_dm_chosen_over_group_when_both_contain_contact(self, monkeypatch):
        """Even when a group chat is returned BEFORE a DM in the query result,
        the resolver must lock onto the DM by chatIdentifier and not the
        group via participant fallback.
        """
        adapter = _make_adapter(monkeypatch)

        async def fake_api_post(path, payload):
            return {
                "data": [
                    {
                        "guid": "iMessage;+;chat0000000000-family-group",
                        "chatIdentifier": "chat0000000000",
                        "participants": [{"address": "user@example.com"}],
                    },
                    {
                        "guid": "iMessage;-;user@example.com",
                        "chatIdentifier": "user@example.com",
                        "participants": [{"address": "user@example.com"}],
                    },
                ]
            }

        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        result = await adapter._resolve_chat_guid("user@example.com")
        assert result == "iMessage;-;user@example.com"

    @pytest.mark.asyncio
    async def test_unresolved_target_is_not_cached(self, monkeypatch):
        """When no exact match is found, the resolver must NOT cache anything.

        Otherwise a later attempt — after the DM has been created — would
        keep returning the stale ``None`` from cache. Also guards against a
        latent variant of #24157 where a group GUID could be cached under a
        bare address key and persist across calls.
        """
        adapter = _make_adapter(monkeypatch)

        async def fake_api_post(path, payload):
            return {
                "data": [
                    {
                        "guid": "iMessage;+;chat0000000000-family-group",
                        "chatIdentifier": "chat0000000000",
                        "participants": [{"address": "user@example.com"}],
                    }
                ]
            }

        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        await adapter._resolve_chat_guid("user@example.com")
        assert "user@example.com" not in adapter._guid_cache


class TestBlueBubblesAttachmentDownload:
    """Verify _download_attachment routes to the correct cache helper."""

    def test_download_image_uses_image_cache(self, monkeypatch):
        """Image MIME routes to cache_image_from_bytes."""
        adapter = _make_adapter(monkeypatch)
        import asyncio

        # Mock the HTTP client response
        class MockResponse:
            status_code = 200
            content = b"\x89PNG\r\n\x1a\n"

            def raise_for_status(self):
                pass

        async def mock_get(*args, **kwargs):
            return MockResponse()

        adapter.client = type("MockClient", (), {"get": mock_get})()

        cached_path = None

        def mock_cache_image(data, ext):
            nonlocal cached_path
            cached_path = f"/tmp/test_image{ext}"
            return cached_path

        monkeypatch.setattr(
            "gateway.platforms.bluebubbles.cache_image_from_bytes",
            mock_cache_image,
        )

        att_meta = {"mimeType": "image/png", "transferName": "photo.png"}
        result = asyncio.get_event_loop().run_until_complete(
            adapter._download_attachment("att-guid-123", att_meta)
        )
        assert result == "/tmp/test_image.png"

    def test_download_audio_uses_audio_cache(self, monkeypatch):
        """Audio MIME routes to cache_audio_from_bytes."""
        adapter = _make_adapter(monkeypatch)
        import asyncio

        class MockResponse:
            status_code = 200
            content = b"fake-audio-data"

            def raise_for_status(self):
                pass

        async def mock_get(*args, **kwargs):
            return MockResponse()

        adapter.client = type("MockClient", (), {"get": mock_get})()

        cached_path = None

        def mock_cache_audio(data, ext):
            nonlocal cached_path
            cached_path = f"/tmp/test_audio{ext}"
            return cached_path

        monkeypatch.setattr(
            "gateway.platforms.bluebubbles.cache_audio_from_bytes",
            mock_cache_audio,
        )

        att_meta = {"mimeType": "audio/mpeg", "transferName": "voice.mp3"}
        result = asyncio.get_event_loop().run_until_complete(
            adapter._download_attachment("att-guid-456", att_meta)
        )
        assert result == "/tmp/test_audio.mp3"

    def test_download_document_uses_document_cache(self, monkeypatch):
        """Non-image/audio MIME routes to cache_document_from_bytes."""
        adapter = _make_adapter(monkeypatch)
        import asyncio

        class MockResponse:
            status_code = 200
            content = b"fake-doc-data"

            def raise_for_status(self):
                pass

        async def mock_get(*args, **kwargs):
            return MockResponse()

        adapter.client = type("MockClient", (), {"get": mock_get})()

        cached_path = None

        def mock_cache_doc(data, filename):
            nonlocal cached_path
            cached_path = f"/tmp/{filename}"
            return cached_path

        monkeypatch.setattr(
            "gateway.platforms.bluebubbles.cache_document_from_bytes",
            mock_cache_doc,
        )

        att_meta = {"mimeType": "application/pdf", "transferName": "report.pdf"}
        result = asyncio.get_event_loop().run_until_complete(
            adapter._download_attachment("att-guid-789", att_meta)
        )
        assert result == "/tmp/report.pdf"

    def test_download_returns_none_without_client(self, monkeypatch):
        """No client → returns None gracefully."""
        adapter = _make_adapter(monkeypatch)
        adapter.client = None
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            adapter._download_attachment("att-guid", {"mimeType": "image/png"})
        )
        assert result is None


# ---------------------------------------------------------------------------
# Webhook registration
# ---------------------------------------------------------------------------


class TestBlueBubblesWebhookUrl:
    """_webhook_url property normalises local hosts to 'localhost'."""

    def test_default_host(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        # Default webhook_host is 0.0.0.0 → normalized to localhost
        assert "localhost" in adapter._webhook_url
        assert str(adapter.webhook_port) in adapter._webhook_url
        assert adapter.webhook_path in adapter._webhook_url

    @pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "localhost", "::"])
    def test_local_hosts_normalized(self, monkeypatch, host):
        adapter = _make_adapter(monkeypatch, webhook_host=host)
        assert adapter._webhook_url.startswith("http://localhost:")

    def test_custom_host_preserved(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_host="192.168.1.50")
        assert "192.168.1.50" in adapter._webhook_url

    def test_register_url_embeds_password(self, monkeypatch):
        """_webhook_register_url should append ?password=... for inbound auth."""
        adapter = _make_adapter(monkeypatch, password="secret123")
        assert adapter._webhook_register_url.endswith("?password=secret123")
        assert adapter._webhook_register_url.startswith(adapter._webhook_url)

    def test_register_url_url_encodes_password(self, monkeypatch):
        """Passwords with special characters must be URL-encoded."""
        adapter = _make_adapter(monkeypatch, password="W9fTC&L5JL*@")
        assert "password=W9fTC%26L5JL%2A%40" in adapter._webhook_register_url

    def test_register_url_for_log_masks_password(self, monkeypatch):
        """Log-safe webhook URLs must never expose the webhook password."""
        adapter = _make_adapter(monkeypatch, password="W9fTC&L5JL*@")
        safe_url = adapter._webhook_register_url_for_log
        assert safe_url.endswith("?password=***")
        assert "W9fTC" not in safe_url
        assert "%26" not in safe_url

    def test_register_url_omits_query_when_no_password(self, monkeypatch):
        """If no password is configured, the register URL should be the bare URL."""
        monkeypatch.delenv("BLUEBUBBLES_PASSWORD", raising=False)
        from gateway.platforms.bluebubbles import BlueBubblesAdapter
        cfg = PlatformConfig(
            enabled=True,
            extra={"server_url": "http://localhost:1234", "password": ""},
        )
        adapter = BlueBubblesAdapter(cfg)
        assert adapter._webhook_register_url == adapter._webhook_url


class TestBlueBubblesWebhookRegistration:
    """Tests for _register_webhook, _unregister_webhook, _find_registered_webhooks."""

    @staticmethod
    def _mock_client(get_response=None, post_response=None, delete_ok=True):
        """Build a tiny mock httpx.AsyncClient."""

        async def mock_get(*args, **kwargs):
            class R:
                status_code = 200
                def raise_for_status(self):
                    pass
                def json(self):
                    return get_response or {"status": 200, "data": []}
            return R()

        async def mock_post(*args, **kwargs):
            class R:
                status_code = 200
                def raise_for_status(self):
                    pass
                def json(self):
                    return post_response or {"status": 200, "data": {}}
            return R()

        async def mock_delete(*args, **kwargs):
            class R:
                status_code = 200 if delete_ok else 500
                def raise_for_status(self_inner):
                    if not delete_ok:
                        raise Exception("delete failed")
            return R()

        return type(
            "MockClient", (),
            {"get": mock_get, "post": mock_post, "delete": mock_delete},
        )()

    # -- _find_registered_webhooks --

    def test_find_registered_webhooks_returns_matches(self, monkeypatch):
        import asyncio
        adapter = _make_adapter(monkeypatch)
        url = adapter._webhook_url
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": [
                {"id": 1, "url": url, "events": ["new-message"]},
                {"id": 2, "url": "http://other:9999/hook", "events": ["message"]},
            ]}
        )
        result = asyncio.get_event_loop().run_until_complete(
            adapter._find_registered_webhooks(url)
        )
        assert len(result) == 1
        assert result[0]["id"] == 1

    def test_register_webhook_collapses_duplicate_matching_routes(self, monkeypatch):
        import asyncio

        adapter = _make_adapter(monkeypatch)
        url = adapter._webhook_register_url
        deleted = []
        posted = []

        class Client:
            async def get(self, *args, **kwargs):
                class R:
                    status_code = 200

                    def raise_for_status(self):
                        pass

                    def json(self):
                        return {
                            "status": 200,
                            "data": [
                                {"id": 1, "url": url, "events": ["new-message"]},
                                {"id": 2, "url": url, "events": ["new-message"]},
                            ],
                        }

                return R()

            async def post(self, *args, **kwargs):
                posted.append((args, kwargs))
                raise AssertionError("existing webhook owner should be reused")

            async def delete(self, url, **kwargs):
                deleted.append(url)

                class R:
                    def raise_for_status(self):
                        pass

                return R()

        adapter.client = Client()  # type: ignore[assignment]

        result = asyncio.get_event_loop().run_until_complete(adapter._register_webhook())

        assert result is True
        assert posted == []
        assert len(deleted) == 1
        assert deleted[0].endswith("/api/v1/webhook/2?password=secret")

    def test_find_registered_webhooks_empty_when_none(self, monkeypatch):
        import asyncio
        adapter = _make_adapter(monkeypatch)
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": []}
        )
        result = asyncio.get_event_loop().run_until_complete(
            adapter._find_registered_webhooks(adapter._webhook_url)
        )
        assert result == []

    def test_find_registered_webhooks_handles_api_error(self, monkeypatch):
        import asyncio
        adapter = _make_adapter(monkeypatch)
        adapter.client = self._mock_client()

        # Override _api_get to raise
        async def bad_get(path):
            raise ConnectionError("server down")
        adapter._api_get = bad_get

        result = asyncio.get_event_loop().run_until_complete(
            adapter._find_registered_webhooks(adapter._webhook_url)
        )
        assert result == []

    # -- _register_webhook --

    def test_register_fresh(self, monkeypatch):
        """No existing webhook → POST creates one."""
        import asyncio
        adapter = _make_adapter(monkeypatch)
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": []},
            post_response={"status": 200, "data": {"id": 42}},
        )
        ok = asyncio.get_event_loop().run_until_complete(
            adapter._register_webhook()
        )
        assert ok is True

    def test_register_accepts_201(self, monkeypatch):
        """BB might return 201 Created — must still succeed."""
        import asyncio
        adapter = _make_adapter(monkeypatch)
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": []},
            post_response={"status": 201, "data": {"id": 43}},
        )
        ok = asyncio.get_event_loop().run_until_complete(
            adapter._register_webhook()
        )
        assert ok is True

    def test_register_reuses_existing(self, monkeypatch):
        """Crash resilience — existing registration is reused, no POST needed."""
        import asyncio
        adapter = _make_adapter(monkeypatch)
        url = adapter._webhook_register_url
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": [
                {"id": 7, "url": url, "events": ["new-message"]},
            ]},
        )

        # Track whether POST was called
        post_called = False
        orig_api_post = adapter._api_post
        async def tracking_post(path, payload):
            nonlocal post_called
            post_called = True
            return await orig_api_post(path, payload)
        adapter._api_post = tracking_post

        ok = asyncio.get_event_loop().run_until_complete(
            adapter._register_webhook()
        )
        assert ok is True
        assert not post_called, "Should reuse existing, not POST again"

    def test_register_returns_false_without_client(self, monkeypatch):
        import asyncio
        adapter = _make_adapter(monkeypatch)
        adapter.client = None
        ok = asyncio.get_event_loop().run_until_complete(
            adapter._register_webhook()
        )
        assert ok is False

    def test_register_returns_false_on_server_error(self, monkeypatch):
        import asyncio
        adapter = _make_adapter(monkeypatch)
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": []},
            post_response={"status": 500, "message": "internal error"},
        )
        ok = asyncio.get_event_loop().run_until_complete(
            adapter._register_webhook()
        )
        assert ok is False

    # -- _unregister_webhook --

    def test_unregister_removes_matching(self, monkeypatch):
        import asyncio
        adapter = _make_adapter(monkeypatch)
        url = adapter._webhook_register_url
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": [
                {"id": 10, "url": url},
            ]},
        )
        ok = asyncio.get_event_loop().run_until_complete(
            adapter._unregister_webhook()
        )
        assert ok is True

    def test_unregister_removes_all_duplicates(self, monkeypatch):
        """Multiple orphaned registrations for same URL — all get removed."""
        import asyncio
        adapter = _make_adapter(monkeypatch)
        url = adapter._webhook_register_url
        deleted_ids = []

        async def mock_delete(*args, **kwargs):
            # Extract ID from URL
            url_str = args[0] if args else ""
            deleted_ids.append(url_str)
            class R:
                status_code = 200
                def raise_for_status(self):
                    pass
            return R()

        adapter.client = self._mock_client(
            get_response={"status": 200, "data": [
                {"id": 1, "url": url},
                {"id": 2, "url": url},
                {"id": 3, "url": "http://other/hook"},
            ]},
        )
        adapter.client.delete = mock_delete

        ok = asyncio.get_event_loop().run_until_complete(
            adapter._unregister_webhook()
        )
        assert ok is True
        assert len(deleted_ids) == 2

    def test_unregister_returns_false_without_client(self, monkeypatch):
        import asyncio
        adapter = _make_adapter(monkeypatch)
        adapter.client = None
        ok = asyncio.get_event_loop().run_until_complete(
            adapter._unregister_webhook()
        )
        assert ok is False

    def test_unregister_handles_api_failure_gracefully(self, monkeypatch):
        import asyncio
        adapter = _make_adapter(monkeypatch)
        adapter.client = self._mock_client()

        async def bad_get(path):
            raise ConnectionError("server down")
        adapter._api_get = bad_get

        ok = asyncio.get_event_loop().run_until_complete(
            adapter._unregister_webhook()
        )
        assert ok is False
