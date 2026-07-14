"""BlueBubbles iMessage platform adapter.

Uses the local BlueBubbles macOS server for outbound REST sends and inbound
webhooks.  Supports text messaging, media attachments (images, voice, video,
documents), tapback reactions, typing indicators, and read receipts.

Architecture based on PR #5869 (benjaminsehl) with inbound attachment
downloading from PR #4588 (YuhangLin).
"""

import asyncio
from collections import OrderedDict
import hashlib
import json
import logging
import os
import random
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
)
from gateway.platforms.helpers import strip_markdown

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WEBHOOK_HOST = "127.0.0.1"
# BlueBubbles webhook events are small JSON/form payloads; attachments come
# through the REST API, not the webhook. 1 MiB is generous headroom while
# keeping oversized/chunked bodies from being buffered unbounded.
_WEBHOOK_MAX_BODY_BYTES = 1_048_576
DEFAULT_WEBHOOK_PORT = 8645
DEFAULT_WEBHOOK_PATH = "/bluebubbles-webhook"
MAX_TEXT_LENGTH = 4000

# BlueBubbles/iMessage does not expose a stable bot mention identity like
# Slack (<@U...>), Telegram (@botname), or Matrix (MXID). When users opt into
# group mention gating without custom aliases, use conservative Hermes wake
# words so `require_mention: true` is a one-line enablement path.
DEFAULT_MENTION_PATTERNS = [
    r"(?<![\w@])@?hermes\s+agent\b[,:\-]?",
    r"(?<![\w@])@?hermes\b[,:\-]?",
]

# Tapback reaction codes (BlueBubbles associatedMessageType values)
_TAPBACK_ADDED = {
    2000: "love", 2001: "like", 2002: "dislike",
    2003: "laugh", 2004: "emphasize", 2005: "question",
}
_TAPBACK_REMOVED = {
    3000: "love", 3001: "like", 3002: "dislike",
    3003: "laugh", 3004: "emphasize", 3005: "question",
}

# Webhook event types that carry user messages
_MESSAGE_EVENTS = {"new-message"}

# Log redaction patterns
_PHONE_RE = re.compile(r"\+?\d{7,15}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

_GUID_CACHE_SIZE = 500  # LRU cap for resolved chat-GUID lookups


def _redact(text: str) -> str:
    """Redact phone numbers and emails from log output."""
    text = _PHONE_RE.sub("[REDACTED]", text)
    text = _EMAIL_RE.sub("[REDACTED]", text)
    return text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_bluebubbles_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


def _normalize_server_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, flags=re.I):
        value = f"http://{value}"
    return value.rstrip("/")





# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class BlueBubblesAdapter(BasePlatformAdapter):
    platform = Platform.BLUEBUBBLES
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = MAX_TEXT_LENGTH
    splits_long_messages = True  # send() chunks via truncate_message(MAX_MESSAGE_LENGTH)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.BLUEBUBBLES)
        extra = config.extra or {}
        self.server_url = _normalize_server_url(
            extra.get("server_url") or os.getenv("BLUEBUBBLES_SERVER_URL", "")
        )
        self.password = extra.get("password") or os.getenv("BLUEBUBBLES_PASSWORD", "")
        self.webhook_host = (
            extra.get("webhook_host")
            or os.getenv("BLUEBUBBLES_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST)
        )
        self.webhook_port = int(
            extra.get("webhook_port")
            or os.getenv("BLUEBUBBLES_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT))
        )
        self.webhook_path = (
            extra.get("webhook_path")
            or os.getenv("BLUEBUBBLES_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH)
        )
        if not str(self.webhook_path).startswith("/"):
            self.webhook_path = f"/{self.webhook_path}"
        self.webhook_public_url = _normalize_server_url(
            extra.get("webhook_public_url")
            or extra.get("webhook_url")
            or os.getenv("BLUEBUBBLES_WEBHOOK_PUBLIC_URL", "")
            or os.getenv("BLUEBUBBLES_WEBHOOK_URL", "")
        )
        self.send_read_receipts = bool(extra.get("send_read_receipts", True))
        self.split_outbound_paragraphs = self._coerce_bool(
            extra.get("split_outbound_paragraphs", os.getenv("BLUEBUBBLES_SPLIT_OUTBOUND_PARAGRAPHS")),
            default=False,
        )
        self.bubble_delay_min_ms = max(0, int(extra.get("bubble_delay_min_ms", 0) or 0))
        self.bubble_delay_max_ms = max(
            self.bubble_delay_min_ms,
            int(extra.get("bubble_delay_max_ms", self.bubble_delay_min_ms) or self.bubble_delay_min_ms),
        )
        self.bubble_typing_chars_per_second = max(
            1.0, float(extra.get("bubble_typing_chars_per_second", 18.0) or 18.0)
        )
        self.webhook_register = self._coerce_bool(
            extra.get("webhook_register", os.getenv("BLUEBUBBLES_WEBHOOK_REGISTER", "true")),
            default=True,
        )
        _group_require_mention = extra.get("group_require_mention")
        if _group_require_mention is None:
            _group_require_mention = os.getenv("BLUEBUBBLES_GROUP_REQUIRE_MENTION")
        if _group_require_mention is None:
            _group_require_mention = extra.get("require_mention")
        if _group_require_mention is None:
            _group_require_mention = os.getenv("BLUEBUBBLES_REQUIRE_MENTION")
        self.group_require_mention = self._coerce_bool(
            _group_require_mention,
            default=True,
        )
        self.require_mention = self.group_require_mention
        self.mention_patterns = self._load_mention_patterns(
            extra.get("mention_patterns") or os.getenv("BLUEBUBBLES_MENTION_PATTERNS", "")
        )
        self._mention_patterns = self._compile_mention_patterns(
            extra["mention_patterns"]
            if "mention_patterns" in extra
            else os.getenv("BLUEBUBBLES_MENTION_PATTERNS")
        )
        self.client: Optional[httpx.AsyncClient] = None
        self._runner = None
        self._registered_webhook = False
        self._private_api_enabled: Optional[bool] = None
        self._helper_connected: bool = False
        self._guid_cache: OrderedDict[str, str] = OrderedDict()
        self._outbound_message_guids: OrderedDict[str, None] = OrderedDict()
        self._outbound_message_guid_limit = int(extra.get("outbound_message_guid_limit", 512) or 512)
        self._seen_message_guids: OrderedDict[str, None] = OrderedDict()
        self._seen_message_guid_limit = int(extra.get("message_dedupe_limit", 2048) or 2048)
        _text_batch_delay = extra.get("text_batch_delay_seconds")
        if _text_batch_delay is None:
            _text_batch_delay = os.getenv("BLUEBUBBLES_TEXT_BATCH_DELAY_SECONDS", "0.8")
        _text_batch_link_delay = extra.get("text_batch_link_delay_seconds")
        if _text_batch_link_delay is None:
            _text_batch_link_delay = os.getenv("BLUEBUBBLES_TEXT_BATCH_LINK_DELAY_SECONDS", "1.5")
        self._text_batch_delay_seconds = float(_text_batch_delay)
        self._text_batch_link_delay_seconds = float(_text_batch_link_delay)
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.server_url}{path}{sep}password={quote(self.password, safe='')}"

    @staticmethod
    def _compile_mention_patterns(raw: Any) -> List[re.Pattern]:
        """Compile group-mention wake words from config/env.

        ``raw`` is a list (from config or env JSON), a string (raw env var:
        JSON list, or comma/newline-separated), or None (use Hermes defaults).
        """
        if raw is None:
            patterns = list(DEFAULT_MENTION_PATTERNS)
        elif isinstance(raw, str):
            text = raw.strip()
            try:
                loaded = json.loads(text) if text else []
            except Exception:
                loaded = None
            patterns = loaded if isinstance(loaded, list) else [
                part.strip()
                for line in text.splitlines()
                for part in line.split(",")
            ]
        elif isinstance(raw, list):
            patterns = raw
        else:
            patterns = [raw]

        compiled: List["re.Pattern"] = []
        for pattern in patterns:
            text = str(pattern).strip()
            if not text:
                continue
            try:
                compiled.append(re.compile(text, re.IGNORECASE))
            except re.error as exc:
                logger.warning("[bluebubbles] Invalid mention pattern %r: %s", text, exc)
        return compiled

    def _message_matches_mention_patterns(self, text: str) -> bool:
        if not text or not self._mention_patterns:
            return False
        return any(pattern.search(text) for pattern in self._mention_patterns)

    def _clean_mention_text(self, text: str) -> str:
        """Strip a leading BlueBubbles wake word before dispatch.

        Custom mention patterns are regular expressions, so stripping only a
        leading match avoids deleting ordinary words later in the prompt.
        """
        if not text:
            return text
        for pattern in self._mention_patterns:
            match = pattern.match(text.lstrip())
            if match:
                cleaned = text.lstrip()[match.end():].lstrip(" ,:-")
                return cleaned or text
        return text

    async def _api_get(self, path: str) -> Dict[str, Any]:
        assert self.client is not None
        res = await self.client.get(self._api_url(path))
        res.raise_for_status()
        return res.json()

    async def _api_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        assert self.client is not None
        res = await self.client.post(self._api_url(path), json=payload)
        res.raise_for_status()
        return res.json()

    async def _refresh_private_api_status(self) -> None:
        """Refresh Private API/helper state without requiring gateway restart."""
        if not self.client:
            return
        try:
            info = await self._api_get("/api/v1/server/info")
            server_data = (info or {}).get("data", {})
            self._private_api_enabled = bool(server_data.get("private_api"))
            self._helper_connected = bool(server_data.get("helper_connected"))
        except Exception:
            pass

    async def _private_api_helper_ready(self) -> bool:
        if not self.client:
            return False
        if not self._private_api_enabled or not self._helper_connected:
            await self._refresh_private_api_status()
        return bool(self._private_api_enabled and self._helper_connected)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.server_url or not self.password:
            logger.error(
                "[bluebubbles] BLUEBUBBLES_SERVER_URL and BLUEBUBBLES_PASSWORD are required"
            )
            return False
        from aiohttp import web

        # Tighter keepalive so idle CLOSE_WAIT drains promptly (#18451).
        from gateway.platforms._http_client_limits import platform_httpx_limits
        self.client = httpx.AsyncClient(timeout=30.0, limits=platform_httpx_limits())
        try:
            await self._api_get("/api/v1/ping")
            info = await self._api_get("/api/v1/server/info")
            server_data = (info or {}).get("data", {})
            self._private_api_enabled = bool(server_data.get("private_api"))
            self._helper_connected = bool(server_data.get("helper_connected"))
            logger.info(
                "[bluebubbles] connected to %s (private_api=%s, helper=%s)",
                self.server_url,
                self._private_api_enabled,
                self._helper_connected,
            )
        except Exception as exc:
            logger.error(
                "[bluebubbles] cannot reach server at %s: %s", self.server_url, exc
            )
            if self.client:
                await self.client.aclose()
                self.client = None
            return False

        if not self.webhook_register:
            self._mark_connected()
            logger.info(
                "[bluebubbles] send-only mode enabled; webhook listener/registration disabled because external router owns BlueBubbles ingress"
            )
            return True

        # Explicit body cap: BlueBubbles webhook events are small JSON (or
        # form-encoded) payloads. client_max_size makes aiohttp enforce the
        # cap on every read path — including chunked requests that carry no
        # Content-Length (same pattern as webhook.py / raft, #58536/#58902).
        app = web.Application(client_max_size=_WEBHOOK_MAX_BODY_BYTES)

        app.router.add_get("/health", lambda _: web.Response(text="ok"))
        app.router.add_post(self.webhook_path, self._handle_webhook)
        # The webhook auth value is carried in the query string because the
        # BlueBubbles webhook API cannot send custom headers. Do not let
        # aiohttp access logs write that request target to agent.log.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
        await site.start()
        self._mark_connected()
        logger.info(
            "[bluebubbles] webhook listening on http://%s:%s%s",
            self.webhook_host,
            self.webhook_port,
            self.webhook_path,
        )

        # Register webhook with BlueBubbles server.  Guest/profile router
        # deployments can set webhook_register=false so exactly one ingress
        # owner registers with BlueBubbles and profile-local adapters only send.
        if self.webhook_register:
            self._registered_webhook = await self._register_webhook()
        else:
            logger.info(
                "[bluebubbles] webhook registration disabled by config; assuming external router owns BlueBubbles ingress"
            )

        return True

    async def disconnect(self) -> None:
        # Unregister webhook before cleaning up, but only if this adapter was
        # the ingress owner that registered it.
        if self.webhook_register and self._registered_webhook:
            await self._unregister_webhook()
            self._registered_webhook = False

        for task in self._pending_text_batch_tasks.values():
            if not task.done():
                task.cancel()
        self._pending_text_batch_tasks.clear()
        self._pending_text_batches.clear()

        if self.client:
            await self.client.aclose()
            self.client = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    @property
    def _webhook_url(self) -> str:
        """Compute the external webhook URL for BlueBubbles registration."""
        host = self.webhook_host
        if host in {"0.0.0.0", "127.0.0.1", "localhost", "::"}:
            host = "localhost"
        local_url = f"http://{host}:{self.webhook_port}{self.webhook_path}"
        return self.webhook_public_url or local_url

    @property
    def _webhook_register_url(self) -> str:
        """Webhook URL registered with BlueBubbles, including the password as
        a query param so inbound webhook POSTs carry credentials.

        BlueBubbles posts events to the exact URL registered via
        ``/api/v1/webhook``. Its webhook registration API does not support
        custom headers, so embedding the password in the URL is the only
        way to authenticate inbound webhooks without disabling auth.
        """
        base = self._webhook_url
        if self.password:
            return f"{base}?password={quote(self.password, safe='')}"
        return base

    @property
    def _webhook_register_url_for_log(self) -> str:
        """Webhook registration URL safe for logs."""
        base = self._webhook_url
        if self.password:
            return f"{base}?password=***"
        return base

    async def _find_registered_webhooks(self, url: str) -> list:
        """Return list of BB webhook entries matching *url*."""
        try:
            res = await self._api_get("/api/v1/webhook")
            data = res.get("data")
            if isinstance(data, list):
                return [wh for wh in data if wh.get("url") == url]
        except Exception:
            pass
        return []

    async def _register_webhook(self) -> bool:
        """Register this webhook URL with the BlueBubbles server.

        BlueBubbles requires webhooks to be registered via API before
        it will send events.  Checks for an existing registration first
        to avoid duplicates (e.g. after a crash without clean shutdown).
        """
        if not self.client:
            return False

        webhook_url = self._webhook_register_url

        # Crash resilience — reuse an existing registration if present, and
        # collapse duplicates so a future profile/router does not receive the
        # same iMessage event multiple times after crashy restarts.
        existing = await self._find_registered_webhooks(webhook_url)
        if existing:
            for duplicate in existing[1:]:
                wh_id = duplicate.get("id")
                if wh_id:
                    try:
                        res = await self.client.delete(
                            self._api_url(f"/api/v1/webhook/{wh_id}")
                        )
                        res.raise_for_status()
                    except Exception:
                        logger.debug(
                            "[bluebubbles] failed to remove duplicate webhook id=%s",
                            wh_id,
                            exc_info=True,
                        )
            logger.info(
                "[bluebubbles] webhook already registered: %s",
                self._webhook_register_url_for_log,
            )
            return True

        payload = {
            "url": webhook_url,
            "events": ["new-message"],
        }

        try:
            res = await self._api_post("/api/v1/webhook", payload)
            status = res.get("status", 0)
            if 200 <= status < 300:
                logger.info(
                    "[bluebubbles] webhook registered with server: %s",
                    self._webhook_register_url_for_log,
                )
                return True
            else:
                logger.warning(
                    "[bluebubbles] webhook registration returned status %s: %s",
                    status,
                    res.get("message"),
                )
                return False
        except Exception as exc:
            logger.warning(
                "[bluebubbles] failed to register webhook with server: %s",
                exc,
            )
            return False

    async def _unregister_webhook(self) -> bool:
        """Unregister this webhook URL from the BlueBubbles server.

        Removes *all* matching registrations to clean up any duplicates
        left by prior crashes.
        """
        if not self.client:
            return False

        webhook_url = self._webhook_register_url
        removed = False

        try:
            for wh in await self._find_registered_webhooks(webhook_url):
                wh_id = wh.get("id")
                if wh_id:
                    res = await self.client.delete(
                        self._api_url(f"/api/v1/webhook/{wh_id}")
                    )
                    res.raise_for_status()
                    removed = True
            if removed:
                logger.info(
                    "[bluebubbles] webhook unregistered: %s",
                    self._webhook_register_url_for_log,
                )
        except Exception as exc:
            logger.debug(
                "[bluebubbles] failed to unregister webhook (non-critical): %s",
                exc,
            )
        return removed

    # ------------------------------------------------------------------
    # Chat GUID resolution
    # ------------------------------------------------------------------

    async def _resolve_chat_guid(self, target: str) -> Optional[str]:
        """Resolve an email/phone to a BlueBubbles chat GUID.

        If *target* already contains a semicolon (raw GUID format like
        ``iMessage;-;user@example.com``), it is returned as-is.  Otherwise
        the adapter queries the BlueBubbles chat list and matches strictly
        on ``chatIdentifier`` / ``identifier``.

        Participant membership is intentionally NOT used as a fallback:
        the same contact can appear in a 1:1 DM and in any number of group
        chats, so a participant match would let an outbound DM reply leak
        into a group thread (see #24157). When no exact chat identity
        matches, return ``None`` and let the caller create a fresh DM
        explicitly via ``_create_chat_for_handle``.
        """
        target = (target or "").strip()
        if not target:
            return None
        # Already a raw GUID
        if ";" in target:
            return target
        if target in self._guid_cache:
            self._guid_cache.move_to_end(target)
            return self._guid_cache[target]
        try:
            payload = await self._api_post(
                "/api/v1/chat/query",
                {"limit": 100, "offset": 0},
            )
            for chat in payload.get("data", []) or []:
                guid = chat.get("guid") or chat.get("chatGuid")
                identifier = chat.get("chatIdentifier") or chat.get("identifier")
                if identifier == target:
                    if guid:
                        self._guid_cache[target] = guid
                        while len(self._guid_cache) > _GUID_CACHE_SIZE:
                            self._guid_cache.popitem(last=False)
                    return guid
        except Exception:
            pass
        return None

    async def resolve_authenticated_existing_dm(
        self, chat_guid: str, expected_participant: str
    ) -> tuple[str, str] | None:
        from gateway.guest_access import normalize_identity

        expected = normalize_identity(expected_participant)
        if not chat_guid or not expected:
            return None
        payload = await self._api_post("/api/v1/chat/query", {"limit": 500, "offset": 0})
        for chat in payload.get("data", []) or []:
            guid = str(chat.get("guid") or chat.get("chatGuid") or "")
            if guid != chat_guid:
                continue
            raw_participants = chat.get("participants") or chat.get("handles") or []
            participants = {
                normalize_identity(
                    item.get("address") or item.get("handle") or item.get("id")
                    if isinstance(item, Mapping) else item
                )
                for item in raw_participants
            }
            participants.discard("")
            if participants != {expected}:
                return None
            fingerprint = hashlib.sha256(
                f"{guid}\0{expected}".encode("utf-8")
            ).hexdigest()
            return guid, fingerprint
        return None

    async def _create_chat_for_handle(
        self, address: str, message: str
    ) -> SendResult:
        """Create a new chat by sending the first message to *address*."""
        payload = {
            "addresses": [address],
            "message": message,
            "tempGuid": f"temp-{datetime.utcnow().timestamp()}",
        }
        try:
            res = await self._api_post("/api/v1/chat/new", payload)
            data = res.get("data") or {}
            msg_id = data.get("guid") or data.get("messageGuid") or "ok"
            return SendResult(success=True, message_id=str(msg_id), raw_response=res)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    @staticmethod
    def _canonical_session_chat_id(
        chat_guid: Optional[str],
        chat_identifier: Optional[str],
        sender: Optional[str],
        *,
        is_group: bool,
    ) -> Optional[str]:
        """Return a stable session/send target for BlueBubbles events.

        BlueBubbles can deliver the same one-to-one iMessage alternately as a
        raw chat GUID (``any;-;+1555...``) and as a bare handle
        (``+1555...``). Use the human handle for DMs so one contact maps to one
        Hermes session. Keep group GUIDs because the participant handle alone
        would collapse unrelated group chats.
        """
        if is_group:
            return chat_guid or chat_identifier or sender
        guid = (chat_guid or "").strip()
        if ";" in guid:
            tail = guid.rsplit(";", 1)[-1].strip()
            if tail:
                return tail.lower() if "@" in tail else tail
        for value in (chat_identifier, sender):
            value = (value or "").strip()
            if value:
                return value.lower() if "@" in value else value
        if not guid:
            return None
        return guid.lower() if "@" in guid else guid

    # ------------------------------------------------------------------
    # Text sending
    # ------------------------------------------------------------------

    def _remember_outbound_message_guid(self, message_id: Optional[str]) -> None:
        if not message_id:
            return
        self._outbound_message_guids[str(message_id)] = None
        self._outbound_message_guids.move_to_end(str(message_id))
        while len(self._outbound_message_guids) > max(1, self._outbound_message_guid_limit):
            self._outbound_message_guids.popitem(last=False)

    def _is_recent_outbound_message_guid(self, message_id: Optional[str]) -> bool:
        if not message_id:
            return False
        key = str(message_id)
        if key not in self._outbound_message_guids:
            return False
        self._outbound_message_guids.move_to_end(key)
        return True

    @staticmethod
    def truncate_message(content: str, max_length: int = MAX_TEXT_LENGTH) -> List[str]:
        # Use the base splitter but skip pagination indicators — iMessage
        # bubbles flow naturally without "(1/3)" suffixes.
        chunks = BasePlatformAdapter.truncate_message(content, max_length)
        return [re.sub(r"\s*\(\d+/\d+\)$", "", c) for c in chunks]

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        text = self.format_message(content)
        if not text:
            return SendResult(success=False, error="BlueBubbles send requires text")
        # iMessage users get a notification per delivered bubble. Keep one
        # assistant response in one bubble by default; only split paragraph
        # breaks when a profile/operator explicitly opts back into that older
        # behavior. Over-length messages still chunk at the platform limit.
        paragraphs = [text]
        if self.split_outbound_paragraphs:
            paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()] or [text]
        chunks: List[str] = []
        for para in paragraphs:
            if len(para) <= self.MAX_MESSAGE_LENGTH:
                chunks.append(para)
            else:
                chunks.extend(self.truncate_message(para, max_length=self.MAX_MESSAGE_LENGTH))
        last = SendResult(success=True)
        delivered_chunks = 0
        for chunk in chunks:
            if delivered_chunks > 0 and self.bubble_delay_max_ms > 0:
                # A burst should feel typed, not atomically dumped. The delay is
                # bounded by profile config and scales gently with bubble length.
                try:
                    await self.send_typing(chat_id)
                except Exception:
                    pass
                jitter = random.uniform(self.bubble_delay_min_ms, self.bubble_delay_max_ms) / 1000.0
                typed = min(2.5, len(chunk) / self.bubble_typing_chars_per_second)
                await asyncio.sleep(jitter + typed)
            guid = await self._resolve_chat_guid(chat_id)
            if not guid:
                # If the target looks like an address, try creating a new chat
                if self._private_api_enabled and (
                    "@" in chat_id or re.match(r"^\+\d+", chat_id)
                ):
                    return await self._create_chat_for_handle(chat_id, chunk)
                return SendResult(
                    success=False,
                    error=f"BlueBubbles chat not found for target: {chat_id}",
                    raw_response={"skip_plaintext_fallback": True},
                )
            payload: Dict[str, Any] = {
                "chatGuid": guid,
                "tempGuid": f"temp-{datetime.utcnow().timestamp()}",
                "message": chunk,
            }
            if reply_to and self._private_api_enabled and self._helper_connected:
                payload["method"] = "private-api"
                payload["selectedMessageGuid"] = reply_to
                payload["partIndex"] = 0
            try:
                assert self.client is not None
                response = await self.client.post(
                    self._api_url("/api/v1/message/text"),
                    json=payload,
                    timeout=120,
                )
                response.raise_for_status()
                res = response.json()
                data = res.get("data") or {}
                msg_id = data.get("guid") or data.get("messageGuid") or "ok"
                last = SendResult(
                    success=True, message_id=str(msg_id), raw_response=res
                )
                self._remember_outbound_message_guid(last.message_id)
                delivered_chunks += 1
            except httpx.ConnectError as exc:
                return SendResult(
                    success=False,
                    error=repr(exc),
                    retryable=delivered_chunks == 0,
                    raw_response={
                        "partial_delivery": delivered_chunks > 0,
                        "skip_plaintext_fallback": True,
                    },
                )
            except httpx.TimeoutException as exc:
                return SendResult(
                    success=False,
                    error=repr(exc),
                    retryable=False,
                    raw_response={
                        "partial_delivery": delivered_chunks > 0,
                        "skip_plaintext_fallback": True,
                    },
                )
            except httpx.TransportError as exc:
                return SendResult(
                    success=False,
                    error=repr(exc),
                    retryable=False,
                    raw_response={
                        "partial_delivery": delivered_chunks > 0,
                        "skip_plaintext_fallback": True,
                    },
                )
            except Exception as exc:
                return SendResult(
                    success=False,
                    error=repr(exc),
                    raw_response={
                        "partial_delivery": delivered_chunks > 0,
                        "skip_plaintext_fallback": True,
                    },
                )
        return last

    # ------------------------------------------------------------------
    # Media sending (outbound)
    # ------------------------------------------------------------------

    async def _send_attachment(
        self,
        chat_id: str,
        file_path: str,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        is_audio_message: bool = False,
    ) -> SendResult:
        """Send a file attachment via BlueBubbles multipart upload."""
        if not self.client:
            return SendResult(success=False, error="Not connected")
        if not os.path.isfile(file_path):
            return SendResult(success=False, error=f"File not found: {file_path}")

        guid = await self._resolve_chat_guid(chat_id)
        if not guid:
            return SendResult(success=False, error=f"Chat not found: {chat_id}")

        fname = filename or os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                files = {"attachment": (fname, f, "application/octet-stream")}
                data: Dict[str, str] = {
                    "chatGuid": guid,
                    "name": fname,
                    "tempGuid": uuid.uuid4().hex,
                }
                if is_audio_message:
                    data["isAudioMessage"] = "true"
                if caption:
                    data["message"] = caption
                    data["text"] = caption
                    data["caption"] = caption
                if reply_to and self._private_api_enabled and self._helper_connected:
                    data["method"] = "private-api"
                    data["selectedMessageGuid"] = reply_to
                    data["partIndex"] = "0"
                res = await self.client.post(
                    self._api_url("/api/v1/message/attachment"),
                    files=files,
                    data=data,
                    timeout=120,
                )
                res.raise_for_status()
                result = res.json()

            if result.get("status") == 200:
                rdata = result.get("data") or {}
                msg_id = rdata.get("guid") if isinstance(rdata, dict) else None
                return SendResult(
                    success=True, message_id=msg_id, raw_response=result
                )
            return SendResult(
                success=False,
                error=result.get("message", "Attachment upload failed"),
            )
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        try:
            from gateway.platforms.base import cache_image_from_url

            local_path = await cache_image_from_url(image_url)
            return await self._send_attachment(chat_id, local_path, caption=caption, reply_to=reply_to)
        except Exception:
            return await super().send_image(chat_id, image_url, caption, reply_to)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_attachment(chat_id, image_path, caption=caption, reply_to=reply_to)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_attachment(
            chat_id, audio_path, caption=caption, reply_to=reply_to, is_audio_message=True
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_attachment(chat_id, video_path, caption=caption, reply_to=reply_to)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_attachment(
            chat_id, file_path, filename=file_name, caption=caption, reply_to=reply_to
        )

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self.send_image(
            chat_id, animation_url, caption, reply_to, metadata
        )

    # ------------------------------------------------------------------
    # Typing indicators
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        client = self.client
        if client is None or not await self._private_api_helper_ready():
            return
        try:
            guid = await self._resolve_chat_guid(chat_id)
            if guid:
                encoded = quote(guid, safe="")
                await client.post(
                    self._api_url(f"/api/v1/chat/{encoded}/typing"), timeout=5
                )
        except Exception:
            pass

    async def stop_typing(self, chat_id: str) -> None:
        client = self.client
        if client is None or not await self._private_api_helper_ready():
            return
        try:
            guid = await self._resolve_chat_guid(chat_id)
            if guid:
                encoded = quote(guid, safe="")
                await client.delete(
                    self._api_url(f"/api/v1/chat/{encoded}/typing"), timeout=5
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Read receipts
    # ------------------------------------------------------------------

    async def mark_read(self, chat_id: str) -> bool:
        client = self.client
        if client is None or not await self._private_api_helper_ready():
            return False
        try:
            guid = await self._resolve_chat_guid(chat_id)
            if guid:
                encoded = quote(guid, safe="")
                await client.post(
                    self._api_url(f"/api/v1/chat/{encoded}/read"), timeout=5
                )
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Tapback reactions
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Chat info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        is_group = ";+;" in (chat_id or "")
        info: Dict[str, Any] = {
            "name": chat_id,
            "type": "group" if is_group else "dm",
        }
        try:
            guid = await self._resolve_chat_guid(chat_id)
            if guid:
                encoded = quote(guid, safe="")
                res = await self._api_get(
                    f"/api/v1/chat/{encoded}?with=participants"
                )
                data = (res or {}).get("data", {})
                display_name = (
                    data.get("displayName")
                    or data.get("chatIdentifier")
                    or chat_id
                )
                participants = []
                for p in data.get("participants", []) or []:
                    addr = (p.get("address") or "").strip()
                    if addr:
                        participants.append(addr)
                info["name"] = display_name
                if participants:
                    info["participants"] = participants
        except Exception:
            pass
        return info

    def format_message(self, content: str) -> str:
        return strip_markdown(content)

    # ------------------------------------------------------------------
    # Inbound attachment downloading (from #4588)
    # ------------------------------------------------------------------

    async def _download_attachment(
        self, att_guid: str, att_meta: Dict[str, Any]
    ) -> Optional[str]:
        """Download an attachment from BlueBubbles and cache it locally.

        Returns the local file path on success, None on failure.
        """
        if not self.client:
            return None
        try:
            encoded = quote(att_guid, safe="")
            resp = await self.client.get(
                self._api_url(f"/api/v1/attachment/{encoded}/download"),
                timeout=60,
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.content

            mime = (att_meta.get("mimeType") or "").lower()
            transfer_name = att_meta.get("transferName", "")

            if mime.startswith("image/"):
                ext_map = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                    "image/heic": ".jpg",
                    "image/heif": ".jpg",
                    "image/tiff": ".jpg",
                }
                ext = ext_map.get(mime, ".jpg")
                return cache_image_from_bytes(data, ext)

            if mime.startswith("audio/"):
                ext_map = {
                    "audio/mp3": ".mp3",
                    "audio/mpeg": ".mp3",
                    "audio/ogg": ".ogg",
                    "audio/wav": ".wav",
                    "audio/x-caf": ".mp3",
                    "audio/mp4": ".m4a",
                    "audio/aac": ".m4a",
                }
                ext = ext_map.get(mime, ".mp3")
                return cache_audio_from_bytes(data, ext)

            # Videos, documents, and everything else
            filename = transfer_name or f"file_{uuid.uuid4().hex[:8]}"
            return cache_document_from_bytes(data, filename)

        except Exception as exc:
            logger.warning(
                "[bluebubbles] failed to download attachment %s: %s",
                _redact(att_guid),
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Webhook handling
    # ------------------------------------------------------------------

    def _extract_payload_record(
        self, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return item
        if isinstance(payload.get("message"), dict):
            return payload.get("message")
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _value(*candidates: Any) -> Optional[str]:
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return default

    @staticmethod
    def _load_mention_patterns(raw: Any) -> List[str]:
        defaults = ["@hermes", "hermes:", "hermes,", "hey hermes"]
        if isinstance(raw, list):
            values = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                values = parsed if isinstance(parsed, list) else raw.split(",")
            except Exception:
                values = raw.split(",")
        else:
            values = defaults
        patterns = [str(v).strip().lower() for v in values if str(v).strip()]
        return patterns or defaults

    def _strip_group_mention(self, text: str) -> Optional[str]:
        stripped = text.strip()
        for pattern in self._mention_patterns:
            match = pattern.match(stripped)
            if match:
                return stripped[match.end():].lstrip(" ,:-") or stripped
        lowered = stripped.lower()
        for pattern in self.mention_patterns:
            if lowered.startswith(pattern):
                return stripped[len(pattern):].lstrip(" ,:-") or stripped
        if lowered.startswith("hermes "):
            return stripped[len("hermes "):].lstrip(" ,:-") or stripped
        return None

    def _dispatch_message_event(self, event: MessageEvent) -> None:
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for iMessage split/link-preview batching."""
        from gateway.session import build_session_key

        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    def _looks_like_link_preview(self, event: MessageEvent) -> bool:
        """Best-effort signal for iOS/BlueBubbles rich previews arriving before typed text."""
        if event.media_urls:
            return True
        raw = event.raw_message or {}
        record = self._extract_payload_record(raw) or raw
        attachments = record.get("attachments") or [] if isinstance(record, dict) else []
        if attachments:
            return True
        text = (event.text or "").strip().lower()
        return "instagram.com" in text or "http://" in text or "https://" in text

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer rapid iMessage text/link-preview webhooks into one agent turn."""
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        if existing is None:
            event._bluebubbles_link_preview = self._looks_like_link_preview(event)  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            if event.text:
                if existing.text == "(attachment)":
                    existing.text = event.text
                else:
                    existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)
            if event.message_id:
                existing.message_id = event.message_id
            if event.reply_to_message_id:
                existing.reply_to_message_id = event.reply_to_message_id
            existing._bluebubbles_link_preview = (  # type: ignore[attr-defined]
                bool(getattr(existing, "_bluebubbles_link_preview", False))
                or self._looks_like_link_preview(event)
            )
            if getattr(event, "_bluebubbles_was_mentioned", False):
                existing._bluebubbles_was_mentioned = True  # type: ignore[attr-defined]

        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(self._flush_text_batch(key))

    async def _flush_text_batch(self, key: str) -> None:
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            delay = self._text_batch_link_delay_seconds if getattr(pending, "_bluebubbles_link_preview", False) else self._text_batch_delay_seconds
            await asyncio.sleep(max(0.0, delay))
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info("[bluebubbles] Flushing text batch %s (%d chars)", _redact(key), len(event.text or ""))
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    async def _handle_webhook(self, request):
        from aiohttp import web

        token = (
            request.query.get("password")
            or request.query.get("guid")
            or request.headers.get("x-password")
            or request.headers.get("x-guid")
            or request.headers.get("x-bluebubbles-guid")
        )
        if token != self.password:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            raw = await request.read()
            body = raw.decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except Exception:
                from urllib.parse import parse_qs

                form = parse_qs(body)
                payload_str = (
                    form.get("payload")
                    or form.get("data")
                    or form.get("message")
                    or [""]
                )[0]
                payload = json.loads(payload_str) if payload_str else {}
        except Exception as exc:
            logger.error("[bluebubbles] webhook parse error: %s", exc)
            return web.json_response({"error": "invalid payload"}, status=400)

        event_type = self._value(payload.get("type"), payload.get("event")) or ""
        # Only process message events; silently acknowledge everything else
        if event_type and event_type not in _MESSAGE_EVENTS:
            return web.Response(text="ok")

        record = self._extract_payload_record(payload) or {}
        is_from_me = bool(
            record.get("isFromMe")
            or record.get("fromMe")
            or record.get("is_from_me")
        )
        if is_from_me:
            return web.Response(text="ok")

        # Skip tapback reactions delivered as messages
        assoc_type = record.get("associatedMessageType")
        if isinstance(assoc_type, int) and assoc_type in {
            **_TAPBACK_ADDED,
            **_TAPBACK_REMOVED,
        }:
            return web.Response(text="ok")

        text = (
            self._value(
                record.get("text"), record.get("message"), record.get("body")
            )
            or ""
        )

        # --- Inbound attachment handling ---
        attachments = record.get("attachments") or []
        media_urls: List[str] = []
        media_types: List[str] = []
        msg_type = MessageType.TEXT

        for att in attachments:
            att_guid = att.get("guid", "")
            if not att_guid:
                continue
            cached = await self._download_attachment(att_guid, att)
            if cached:
                mime = (att.get("mimeType") or "").lower()
                media_urls.append(cached)
                media_types.append(mime)
                if mime.startswith("image/"):
                    msg_type = MessageType.PHOTO
                elif mime.startswith("audio/") or (att.get("uti") or "").endswith(
                    "caf"
                ):
                    msg_type = MessageType.VOICE
                elif mime.startswith("video/"):
                    msg_type = MessageType.VIDEO
                else:
                    msg_type = MessageType.DOCUMENT

        # With multiple attachments, prefer PHOTO if any images present
        if len(media_urls) > 1:
            mime_prefixes = {(m or "").split("/")[0] for m in media_types}
            if "image" in mime_prefixes:
                msg_type = MessageType.PHOTO

        if not text and media_urls:
            text = "(attachment)"
        # --- End attachment handling ---

        chat_guid = self._value(
            record.get("chatGuid"),
            payload.get("chatGuid"),
            record.get("chat_guid"),
            payload.get("chat_guid"),
            payload.get("guid"),
        )
        # Fallback: BlueBubbles v1.9+ webhook payloads omit top-level chatGuid;
        # the chat GUID is nested under data.chats[0].guid instead.
        if not chat_guid:
            _chats = record.get("chats") or []
            if _chats and isinstance(_chats[0], dict):
                chat_guid = _chats[0].get("guid") or _chats[0].get("chatGuid")
        chat_identifier = self._value(
            record.get("chatIdentifier"),
            record.get("identifier"),
            payload.get("chatIdentifier"),
            payload.get("identifier"),
        )
        sender = (
            self._value(
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
        if not sender or not (chat_guid or chat_identifier) or not text:
            return web.json_response({"error": "missing message fields"}, status=400)

        is_group = bool(record.get("isGroup")) or (";+;" in (chat_guid or ""))
        session_chat_id = self._canonical_session_chat_id(
            chat_guid,
            chat_identifier,
            sender,
            is_group=is_group,
        )
        if not session_chat_id:
            return web.json_response({"error": "missing chat target"}, status=400)
        if not is_group and chat_guid and ";" in chat_guid:
            self._guid_cache[session_chat_id] = chat_guid
        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=chat_identifier or sender,
            chat_type="group" if is_group else "dm",
            user_id=sender,
            user_name=sender,
            chat_id_alt=chat_identifier,
        )
        message_id = self._value(
            record.get("guid"),
            record.get("messageGuid"),
            record.get("id"),
        )
        if message_id:
            message_id = str(message_id)
            if message_id in self._seen_message_guids:
                self._seen_message_guids.move_to_end(message_id)
                return web.Response(text="ok")
            self._seen_message_guids[message_id] = None
            while len(self._seen_message_guids) > max(1, self._seen_message_guid_limit):
                self._seen_message_guids.popitem(last=False)
        reply_to_message_id = self._value(
            record.get("threadOriginatorGuid"),
            record.get("associatedMessageGuid"),
        )
        mentioned_text = None
        if is_group and self.group_require_mention:
            mentioned_text = self._strip_group_mention(text)
            # iMessage has no real bot mention primitive. Treat replies to a
            # recent Hermes outbound bubble as addressed, but keep ordinary
            # inline replies to other humans as observed-only group context.
            reply_addresses_hermes = self._is_recent_outbound_message_guid(reply_to_message_id)
            if mentioned_text is None and not reply_addresses_hermes:
                event = MessageEvent(
                    text=text,
                    message_type=msg_type,
                    source=source,
                    raw_message=payload,
                    message_id=message_id,
                    media_urls=media_urls,
                    media_types=media_types,
                    channel_prompt=(
                        "Observed group context may be present; treat it as background only."
                    ),
                    observed_only=True,
                )
                if self._message_handler:
                    task = asyncio.ensure_future(self._message_handler(event))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                return web.Response(text="ok")
            if mentioned_text is not None:
                text = mentioned_text
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=payload,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            media_urls=media_urls,
            media_types=media_types,
            channel_prompt=(
                "Observed group context may be present; treat it as background only."
                if is_group else None
            ),
        )
        if is_group and mentioned_text is not None:
            event._bluebubbles_was_mentioned = True  # type: ignore[attr-defined]
        if (event.message_type == MessageType.TEXT or event.media_urls) and self._text_batch_delay_seconds > 0:
            self._enqueue_text_event(event)
        else:
            self._dispatch_message_event(event)

        # Fire-and-forget read receipt
        if self.send_read_receipts and session_chat_id:
            asyncio.create_task(self.mark_read(session_chat_id))

        return web.Response(text="ok")
