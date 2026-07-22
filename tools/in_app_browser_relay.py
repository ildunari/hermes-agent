"""Token-authenticated one-page CDP relay for Hermes in-app browser sessions.

The relay deliberately presents a browser-shaped CDP endpoint to the two existing
Hermes consumers while exposing exactly one synthetic page. Browser-wide and
target-lifecycle authority stays inside the relay; callers never receive the
upstream target or session identifiers.

This module owns only the local loopback containment seam. The gateway/Desktop
transport adapter supplied as ``upstream_url`` remains responsible for the
D-015 owner fence and socketless debugger dispatch.
"""
from __future__ import annotations

import asyncio
from collections import deque
import hmac
import json
import secrets
import threading
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Protocol
from urllib.parse import quote

import websockets

MIB = 1024 * 1024
RELAY_MESSAGE_CAP = 50 * MIB
SYNTHETIC_TARGET_ID = "hermes-page-1"
SYNTHETIC_BROWSER_VERSION = {
    "protocolVersion": "1.3",
    "product": "Hermes/one-page-relay",
    "revision": "relay-local",
    "userAgent": "Hermes one-page relay",
    "jsVersion": "0",
}

_BROWSER_DENIED = frozenset(
    {
        "Browser.close",
        "Browser.getBrowserCommandLine",
        "Browser.getHistograms",
        "Browser.getHistogram",
        "Browser.getWindowBounds",
        "Browser.getWindowForTarget",
        "Browser.grantPermissions",
        "Browser.resetPermissions",
        "Browser.setDownloadBehavior",
        "Browser.setPermission",
        "Browser.setWindowBounds",
    }
)
_TARGET_DENIED = frozenset(
    {
        "Target.activateTarget",
        "Target.closeTarget",
        "Target.createBrowserContext",
        "Target.createTarget",
        "Target.disposeBrowserContext",
        "Target.getBrowserContexts",
    }
)
_UPLOAD_DENIED = frozenset({"DOM.setFileInputFiles"})
_DOWNLOAD_DENIED = frozenset({"Page.setDownloadBehavior"})


def _message_size(raw: str | bytes) -> int:
    return len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)


def _request_path(connection: Any) -> str:
    request = getattr(connection, "request", None)
    path = getattr(request, "path", None)
    if isinstance(path, str):
        return path.split("?", 1)[0]
    legacy = getattr(connection, "path", None)
    return legacy.split("?", 1)[0] if isinstance(legacy, str) else ""


@dataclass(frozen=True)
class _PendingRequest:
    method: str
    frame_id: str | None = None
    session_id: str | None = None


class SocketlessFrameDuplex(Protocol):
    """One socketless CDP peer used by the gateway/Electron transport adapter."""

    async def send(self, raw: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    def consumer_disconnected(self) -> bool: ...


class OnePageRelay:
    """A bounded, token-authenticated loopback WebSocket CDP relay.

    ``start``/``stop`` are synchronous because agent-browser and
    :class:`tools.browser_supervisor.CDPSupervisor` are synchronous consumers.
    The server itself runs on a private event-loop thread and binds directly to
    ``127.0.0.1:0`` so no probe/rebind race exists.
    """

    def __init__(
        self,
        upstream_url: str | SocketlessFrameDuplex,
        *,
        token: str | None = None,
        on_top_level_commit: Callable[[], None] | None = None,
    ) -> None:
        if isinstance(upstream_url, str):
            if not upstream_url:
                raise ValueError("one-page relay requires an upstream WebSocket URL")
        elif not callable(getattr(upstream_url, "send", None)) or not callable(
            getattr(upstream_url, "__aiter__", None)
        ):
            raise ValueError("one-page relay requires a WebSocket URL or socketless frame duplex")
        self.upstream_url = upstream_url
        self._token = token or secrets.token_urlsafe(32)
        if len(self._token) < 43:
            raise ValueError("one-page relay token must contain at least 256 bits")
        self.url = ""
        self.trace: list[tuple[str, str]] = []
        self.cancelled_pending = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hermes-one-page-relay")
        self._ready = threading.Event()
        self._server: Any = None
        self._stop: asyncio.Event | None = None
        self._capture_lock = threading.Lock()
        self._accessibility_snapshots: deque[dict[str, Any]] = deque()
        self._top_frame_id: str | None = None
        self._on_top_level_commit = on_top_level_commit

    def take_accessibility_snapshots(self) -> list[dict[str, Any]]:
        """Drain exact AX responses used by the immediately preceding snapshot.

        ``agent-browser`` strips backend-node identity from its public JSON in
        current releases.  Capturing the response at this relay seam preserves
        the identity from the same CDP command, avoiding a racy second tree read.
        """

        with self._capture_lock:
            snapshots = list(self._accessibility_snapshots)
            self._accessibility_snapshots.clear()
            return snapshots

    def _capture_accessibility(
        self, pending: _PendingRequest | None, message: dict[str, Any]
    ) -> None:
        if pending is None or pending.method != "Accessibility.getFullAXTree":
            return
        result = message.get("result")
        nodes = result.get("nodes") if isinstance(result, dict) else None
        if not isinstance(nodes, list):
            return
        capture = {
            "frame_id": pending.frame_id or self._top_frame_id or SYNTHETIC_TARGET_ID,
            "nodes": nodes,
        }
        with self._capture_lock:
            self._accessibility_snapshots.append(capture)

    def _observe_frame_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        committed = False
        if method == "Page.frameNavigated":
            frame = params.get("frame")
            if isinstance(frame, dict):
                frame_id = frame.get("id")
                if isinstance(frame_id, str) and frame_id and not frame.get("parentId"):
                    self._top_frame_id = frame_id
                    committed = True
        elif method == "Page.navigatedWithinDocument":
            frame_id = params.get("frameId")
            committed = (
                isinstance(frame_id, str)
                and bool(frame_id)
                and frame_id == self._top_frame_id
            )
        if committed and self._on_top_level_commit is not None:
            try:
                self._on_top_level_commit()
            except Exception:
                # Ref invalidation is fail-dark state maintenance; it must never
                # break the CDP stream itself.
                pass

    @staticmethod
    def admit_frame(raw: str | bytes) -> bool:
        return _message_size(raw) <= RELAY_MESSAGE_CAP

    def start(self, *, timeout: float = 10) -> None:
        if self._thread.is_alive() or self.url:
            raise RuntimeError("one-page relay already started")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("one-page relay did not start")

    def stop(self, *, timeout: float = 10) -> None:
        if self._loop.is_running() and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError("one-page relay did not stop")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._stop = asyncio.Event()
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        self._server = await websockets.serve(
            self._handle,
            "127.0.0.1",
            0,
            max_size=None,
            compression=None,
            close_timeout=1,
        )
        port = next(iter(self._server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/{quote(self._token, safe='')}"
        self._ready.set()
        assert self._stop is not None
        await self._stop.wait()
        self._server.close()
        await self._server.wait_closed()

    @staticmethod
    def _denied(method: str, reason: str) -> str:
        return f"{reason}: {method} is outside the one-page relay capability"

    async def _send_error(self, downstream: Any, request_id: Any, method: str, reason: str) -> None:
        await downstream.send(
            json.dumps(
                {
                    "id": request_id,
                    "error": {"code": -32040, "message": self._denied(method, reason)},
                },
                separators=(",", ":"),
            )
        )
        self.trace.append(("deny", method))

    async def _handle(self, downstream: Any) -> None:
        supplied = _request_path(downstream).removeprefix("/")
        if not hmac.compare_digest(supplied.encode("utf-8"), self._token.encode("utf-8")):
            self.trace.append(("deny", "authentication"))
            await downstream.close(code=4403, reason="relay authentication failed")
            return

        if not isinstance(self.upstream_url, str):
            await self._handle_socketless(downstream, self.upstream_url)
            return

        pending: dict[int | str, _PendingRequest] = {}
        page_target: str | None = None
        real_to_synthetic_sessions: dict[str, str] = {}
        synthetic_to_real_sessions: dict[str, str] = {}
        next_session = 0

        async with websockets.connect(
            self.upstream_url, max_size=None, compression=None, close_timeout=1
        ) as upstream:
            async def client_to_browser() -> None:
                nonlocal page_target
                async for raw in downstream:
                    if not self.admit_frame(raw):
                        request_id = None
                        try:
                            decoded = json.loads(raw)
                            request_id = decoded.get("id") if isinstance(decoded, dict) else None
                        except Exception:
                            pass
                        await downstream.send(
                            json.dumps(
                                {
                                    "id": request_id,
                                    "error": {
                                        "code": -32050,
                                        "message": "CDP_PROTOCOL_ERROR: relay frame exceeds 50 MiB",
                                    },
                                },
                                separators=(",", ":"),
                            )
                        )
                        self.trace.append(("deny", "over-cap"))
                        continue

                    try:
                        request = json.loads(raw)
                    except (TypeError, ValueError):
                        await self._send_error(downstream, None, "", "CDP_PROTOCOL_ERROR")
                        continue
                    if not isinstance(request, dict):
                        await self._send_error(downstream, None, "", "CDP_PROTOCOL_ERROR")
                        continue
                    method = request.get("method")
                    params = request.get("params") or {}
                    request_id = request.get("id")
                    if not isinstance(method, str) or not isinstance(params, dict):
                        await self._send_error(downstream, request_id, "", "CDP_PROTOCOL_ERROR")
                        continue

                    if method == "Browser.getVersion":
                        await downstream.send(
                            json.dumps({"id": request_id, "result": SYNTHETIC_BROWSER_VERSION}, separators=(",", ":"))
                        )
                        self.trace.append(("synthesize", method))
                        continue
                    if method.startswith("Browser.") or method in _BROWSER_DENIED:
                        await self._send_error(downstream, request_id, method, "BROWSER_METHOD_BLOCKED")
                        continue
                    if method in _TARGET_DENIED:
                        await self._send_error(downstream, request_id, method, "TARGET_METHOD_BLOCKED")
                        continue
                    if method in _UPLOAD_DENIED:
                        await self._send_error(downstream, request_id, method, "UPLOAD_UNSUPPORTED")
                        continue
                    if method in _DOWNLOAD_DENIED:
                        await self._send_error(downstream, request_id, method, "DOWNLOAD_BLOCKED")
                        continue
                    if method == "Target.attachToTarget":
                        if params.get("targetId") != SYNTHETIC_TARGET_ID:
                            await self._send_error(downstream, request_id, method, "TARGET_NOT_OWNED")
                            continue
                        if page_target:
                            request["params"] = {**params, "targetId": page_target}

                    session_id = request.get("sessionId")
                    if session_id is not None:
                        real_session = synthetic_to_real_sessions.get(session_id)
                        if real_session is None:
                            await self._send_error(downstream, request_id, method, "TARGET_NOT_OWNED")
                            continue
                        request["sessionId"] = real_session

                    if request_id is not None:
                        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
                            await self._send_error(downstream, None, method, "CDP_PROTOCOL_ERROR")
                            continue
                        pending[request_id] = _PendingRequest(
                            method,
                            frame_id=(params.get("frameId") if isinstance(params.get("frameId"), str) else None),
                            session_id=(request.get("sessionId") if isinstance(request.get("sessionId"), str) else None),
                        )
                    self.trace.append(("send", method))
                    await upstream.send(json.dumps(request, separators=(",", ":")))

            async def browser_to_client() -> None:
                nonlocal page_target, next_session
                async for raw in upstream:
                    if not self.admit_frame(raw):
                        self.trace.append(("deny", "upstream-over-cap"))
                        continue
                    try:
                        message = json.loads(raw)
                    except (TypeError, ValueError):
                        self.trace.append(("deny", "upstream-invalid-json"))
                        continue
                    if not isinstance(message, dict):
                        continue
                    response_id = message.get("id")
                    pending_request = (
                        pending.pop(response_id, None)
                        if isinstance(response_id, (int, str)) and not isinstance(response_id, bool)
                        else None
                    )
                    method = pending_request.method if pending_request else ""
                    self._capture_accessibility(pending_request, message)
                    if method == "Target.getTargets" and "result" in message:
                        targets = message["result"].get("targetInfos", [])
                        pages = [target for target in targets if target.get("type") == "page"]
                        if not pages:
                            message = {
                                "id": message.get("id"),
                                "error": {"code": -32041, "message": "NO_ACTIVE_TAB: upstream has no page"},
                            }
                        else:
                            page_target = pages[0]["targetId"]
                            message["result"]["targetInfos"] = [
                                {**pages[0], "targetId": SYNTHETIC_TARGET_ID, "type": "page"}
                            ]
                    if method == "Target.attachToTarget" and "result" in message:
                        real_session = message["result"].get("sessionId")
                        if isinstance(real_session, str) and real_session:
                            next_session += 1
                            synthetic_session = f"hermes-session-{next_session}"
                            real_to_synthetic_sessions[real_session] = synthetic_session
                            synthetic_to_real_sessions[synthetic_session] = real_session
                            message["result"]["sessionId"] = synthetic_session
                    if method == "Page.getFrameTree" and "result" in message:
                        frame = ((message.get("result") or {}).get("frameTree") or {}).get("frame")
                        frame_id = frame.get("id") if isinstance(frame, dict) else None
                        if isinstance(frame_id, str) and frame_id:
                            self._top_frame_id = frame_id

                    real_session = message.get("sessionId")
                    if isinstance(real_session, str):
                        synthetic_session = real_to_synthetic_sessions.get(real_session)
                        if synthetic_session is None:
                            self.trace.append(("deny", "unowned-session-event"))
                            continue
                        message["sessionId"] = synthetic_session
                    info = (message.get("params") or {}).get("targetInfo")
                    if isinstance(info, dict):
                        if info.get("targetId") == page_target:
                            info["targetId"] = SYNTHETIC_TARGET_ID
                        elif "targetId" in info:
                            # Discovery for an unowned target is not part of the
                            # one-page capability and cannot be safely rewritten.
                            continue
                    self._observe_frame_message(message)
                    await downstream.send(json.dumps(message, separators=(",", ":")))

            left = asyncio.create_task(client_to_browser())
            right = asyncio.create_task(browser_to_client())
            done, open_tasks = await asyncio.wait({left, right}, return_when=asyncio.FIRST_COMPLETED)
            if pending:
                self.cancelled_pending += len(pending)
            for task in open_tasks:
                task.cancel()
            await asyncio.gather(*done, *open_tasks, return_exceptions=True)

    async def _handle_socketless(self, downstream: Any, upstream: SocketlessFrameDuplex) -> None:
        """Expose a browser-shaped endpoint over one already-attached debugger."""

        pending_requests: dict[int | str, _PendingRequest] = {}
        synthetic_sessions: set[str] = set()
        next_session = 0

        async def client_to_browser() -> None:
            nonlocal next_session
            async for raw in downstream:
                if not self.admit_frame(raw):
                    request_id = None
                    try:
                        decoded = json.loads(raw)
                        request_id = decoded.get("id") if isinstance(decoded, dict) else None
                    except Exception:
                        pass
                    await downstream.send(
                        json.dumps(
                            {
                                "id": request_id,
                                "error": {
                                    "code": -32050,
                                    "message": "CDP_PROTOCOL_ERROR: relay frame exceeds 50 MiB",
                                },
                            },
                            separators=(",", ":"),
                        )
                    )
                    self.trace.append(("deny", "over-cap"))
                    continue
                try:
                    request = json.loads(raw)
                except (TypeError, ValueError):
                    await self._send_error(downstream, None, "", "CDP_PROTOCOL_ERROR")
                    continue
                if not isinstance(request, dict):
                    await self._send_error(downstream, None, "", "CDP_PROTOCOL_ERROR")
                    continue
                method = request.get("method")
                params = request.get("params") or {}
                request_id = request.get("id")
                if not isinstance(method, str) or not isinstance(params, dict):
                    await self._send_error(downstream, request_id, "", "CDP_PROTOCOL_ERROR")
                    continue
                if request_id is not None and (
                    not isinstance(request_id, (int, str)) or isinstance(request_id, bool)
                ):
                    await self._send_error(downstream, None, method, "CDP_PROTOCOL_ERROR")
                    continue

                if method == "Browser.getVersion":
                    await downstream.send(
                        json.dumps({"id": request_id, "result": SYNTHETIC_BROWSER_VERSION}, separators=(",", ":"))
                    )
                    self.trace.append(("synthesize", method))
                    continue
                if method.startswith("Browser.") or method in _BROWSER_DENIED:
                    await self._send_error(downstream, request_id, method, "BROWSER_METHOD_BLOCKED")
                    continue
                if method in _TARGET_DENIED:
                    await self._send_error(downstream, request_id, method, "TARGET_METHOD_BLOCKED")
                    continue
                if method in _UPLOAD_DENIED:
                    await self._send_error(downstream, request_id, method, "UPLOAD_UNSUPPORTED")
                    continue
                if method in _DOWNLOAD_DENIED:
                    await self._send_error(downstream, request_id, method, "DOWNLOAD_BLOCKED")
                    continue
                if method == "Target.getTargets":
                    await downstream.send(
                        json.dumps(
                            {
                                "id": request_id,
                                "result": {
                                    "targetInfos": [
                                        {
                                            "attached": True,
                                            "browserContextId": "hermes-browser-context",
                                            "targetId": SYNTHETIC_TARGET_ID,
                                            "title": "Hermes in-app browser",
                                            "type": "page",
                                            "url": "about:blank",
                                        }
                                    ]
                                },
                            },
                            separators=(",", ":"),
                        )
                    )
                    self.trace.append(("synthesize", method))
                    continue
                if method == "Target.attachToTarget":
                    if params.get("targetId") != SYNTHETIC_TARGET_ID:
                        await self._send_error(downstream, request_id, method, "TARGET_NOT_OWNED")
                        continue
                    next_session += 1
                    synthetic_session = f"hermes-session-{next_session}"
                    synthetic_sessions.add(synthetic_session)
                    await downstream.send(
                        json.dumps(
                            {"id": request_id, "result": {"sessionId": synthetic_session}},
                            separators=(",", ":"),
                        )
                    )
                    self.trace.append(("synthesize", method))
                    continue

                if request_id is None:
                    await self._send_error(downstream, None, method, "CDP_PROTOCOL_ERROR")
                    continue

                synthetic_session = request.get("sessionId")
                if synthetic_session is not None:
                    if not isinstance(synthetic_session, str) or synthetic_session not in synthetic_sessions:
                        await self._send_error(downstream, request_id, method, "TARGET_NOT_OWNED")
                        continue
                    request.pop("sessionId", None)
                if request_id is not None:
                    pending_requests[request_id] = _PendingRequest(
                        method,
                        frame_id=(params.get("frameId") if isinstance(params.get("frameId"), str) else None),
                        session_id=synthetic_session,
                    )
                self.trace.append(("send", method))
                await upstream.send(json.dumps(request, separators=(",", ":")))

        async def browser_to_client() -> None:
            async for raw in upstream:
                if not self.admit_frame(raw):
                    self.trace.append(("deny", "upstream-over-cap"))
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    self.trace.append(("deny", "upstream-invalid-json"))
                    continue
                if not isinstance(message, dict):
                    continue
                response_id = message.get("id")
                pending = None
                if isinstance(response_id, (int, str)) and not isinstance(response_id, bool):
                    pending = pending_requests.pop(response_id, None)
                    if pending and pending.session_id:
                        message["sessionId"] = pending.session_id
                elif response_id is None and synthetic_sessions:
                    # Electron debugger events carry no session id. Restore the
                    # sole relay-minted session before exposing the event.
                    message["sessionId"] = next(iter(synthetic_sessions))
                self._capture_accessibility(pending, message)
                if pending and pending.method == "Page.getFrameTree" and "result" in message:
                    frame = ((message.get("result") or {}).get("frameTree") or {}).get("frame")
                    frame_id = frame.get("id") if isinstance(frame, dict) else None
                    if isinstance(frame_id, str) and frame_id:
                        self._top_frame_id = frame_id
                self._observe_frame_message(message)
                await downstream.send(json.dumps(message, separators=(",", ":")))

        left = asyncio.create_task(client_to_browser())
        right = asyncio.create_task(browser_to_client())
        done, open_tasks = await asyncio.wait({left, right}, return_when=asyncio.FIRST_COMPLETED)
        if pending_requests:
            self.cancelled_pending += len(pending_requests)
            # The local CDP consumer vanished with outstanding work. The
            # gateway owns terminal certainty and exact-relay teardown.
            upstream.consumer_disconnected()
        for task in open_tasks:
            task.cancel()
        await asyncio.gather(*done, *open_tasks, return_exceptions=True)


__all__ = [
    "MIB",
    "OnePageRelay",
    "RELAY_MESSAGE_CAP",
    "SYNTHETIC_BROWSER_VERSION",
    "SYNTHETIC_TARGET_ID",
    "SocketlessFrameDuplex",
]
