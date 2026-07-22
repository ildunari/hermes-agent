"""Executable Phase-0 conformance fixture for the in-app-browser CDP relay.

This is deliberately a test-only fixture, not a second product relay.  It puts the
real Rust ``agent-browser`` CLI and Hermes's real Python ``CDPSupervisor`` on the
same one-page, size-bounded CDP seam.  E2 can point
``HERMES_AGENT_BROWSER_0260`` and ``HERMES_INSTALLED_AGENT_BROWSER_0320`` at exact binaries;
when unset, an exact locally installed matching binary is used and unavailable
versions are reported as skips rather than silently substituted.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import websockets

from tools.in_app_browser_relay import OnePageRelay as ProductionOnePageRelay
from tools.browser_supervisor import CDPSupervisor

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
BROWSER_TOOL_NAMES = (
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
    "browser_scroll", "browser_back", "browser_press", "browser_get_images",
    "browser_vision", "browser_console", "browser_cdp", "browser_dialog",
)

PAGE_HTML = """<!doctype html><meta charset=utf-8><title>Hermes relay fixture</title>
<label>Name <input id=name aria-label=Name></label>
<button id=apply onclick="document.querySelector('h1').textContent=name.value">Apply</button>
<h1>ready</h1><script>console.log('relay-fixture-ready')</script>"""
PAGE_URL = "data:text/html;base64," + base64.b64encode(PAGE_HTML.encode()).decode()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class OnePageRelay:
    """Small executable conformance peer enforcing the selected relay contract."""

    def __init__(self, upstream_url: str) -> None:
        self.upstream_url = upstream_url
        self.url = ""
        self.trace: list[tuple[str, str]] = []
        self.cancelled_pending = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._server = None
        self._stop = None

    @staticmethod
    def admit_frame(raw: str | bytes) -> bool:
        size = len(raw.encode() if isinstance(raw, str) else raw)
        return size <= RELAY_MESSAGE_CAP

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(10), "relay did not start"

    def stop(self) -> None:
        if self._loop.is_running() and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)
        assert not self._thread.is_alive(), "relay did not stop"

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._stop = asyncio.Event()
        self._loop.run_until_complete(self._serve())
        self._loop.close()

    async def _serve(self) -> None:
        self._server = await websockets.serve(
            self._handle, "127.0.0.1", 0, max_size=None, compression=None
        )
        port = next(iter(self._server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        self._ready.set()
        assert self._stop is not None
        await self._stop.wait()
        self._server.close()
        await self._server.wait_closed()

    @staticmethod
    def _denied(method: str, reason: str) -> str:
        return f"{reason}: {method} is outside the one-page relay capability"

    async def _handle(self, downstream) -> None:
        pending: dict[int, str] = {}
        page_target: str | None = None
        async with websockets.connect(
            self.upstream_url, max_size=None, compression=None
        ) as upstream:
            async def client_to_browser() -> None:
                nonlocal page_target
                async for raw in downstream:
                    if not self.admit_frame(raw):
                        request_id = None
                        try:
                            request_id = json.loads(raw).get("id")
                        except Exception:
                            pass
                        await downstream.send(json.dumps({
                            "id": request_id,
                            "error": {"code": -32050, "message": "CDP_PROTOCOL_ERROR: relay frame exceeds 50 MiB"},
                        }))
                        self.trace.append(("deny", "over-cap"))
                        continue
                    request = json.loads(raw)
                    method = request.get("method", "")
                    params = request.get("params") or {}
                    request_id = request.get("id")
                    reason = None
                    # agent-browser 0.32.0 uses Browser.getVersion solely as a
                    # liveness probe before reusing an existing --cdp daemon.
                    # Answer from relay-local constants: forwarding this
                    # browser-wide method would weaken one-page containment,
                    # while refusing it makes 0.32.0 reconnect and clear refs.
                    if method == "Browser.getVersion":
                        await downstream.send(json.dumps({
                            "id": request_id,
                            "result": SYNTHETIC_BROWSER_VERSION,
                        }))
                        self.trace.append(("synthesize", method))
                        continue
                    if method.startswith("Browser."):
                        reason = "BROWSER_METHOD_BLOCKED"
                    elif method in {"Target.createTarget", "Target.activateTarget", "Target.closeTarget"}:
                        reason = "TARGET_METHOD_BLOCKED"
                    elif method in {"DOM.setFileInputFiles", "Page.setDownloadBehavior", "Browser.setDownloadBehavior"}:
                        reason = "UPLOAD_UNSUPPORTED" if "File" in method else "DOWNLOAD_BLOCKED"
                    elif method == "Target.attachToTarget":
                        if params.get("targetId") != SYNTHETIC_TARGET_ID:
                            reason = "TARGET_NOT_OWNED"
                        elif page_target:
                            params = dict(params, targetId=page_target)
                            request["params"] = params
                    if reason:
                        await downstream.send(json.dumps({
                            "id": request_id,
                            "error": {"code": -32040, "message": self._denied(method, reason)},
                        }))
                        self.trace.append(("deny", method))
                        continue
                    if request_id is not None:
                        pending[request_id] = method
                    self.trace.append(("send", method))
                    await upstream.send(json.dumps(request, separators=(",", ":")))

            async def browser_to_client() -> None:
                nonlocal page_target
                async for raw in upstream:
                    if not self.admit_frame(raw):
                        self.trace.append(("deny", "upstream-over-cap"))
                        continue
                    message = json.loads(raw)
                    method = pending.pop(message.get("id"), "")
                    if method == "Target.getTargets" and "result" in message:
                        targets = message["result"].get("targetInfos", [])
                        pages = [t for t in targets if t.get("type") == "page"]
                        if not pages:
                            message = {"id": message.get("id"), "error": {
                                "code": -32041, "message": "NO_ACTIVE_TAB: upstream has no page"
                            }}
                        else:
                            page_target = pages[0]["targetId"]
                            owned = dict(pages[0], targetId=SYNTHETIC_TARGET_ID, type="page")
                            message["result"]["targetInfos"] = [owned]
                    # Never leak the real target id in discovery events.
                    info = (message.get("params") or {}).get("targetInfo")
                    if isinstance(info, dict) and info.get("targetId") == page_target:
                        info["targetId"] = SYNTHETIC_TARGET_ID
                    await downstream.send(json.dumps(message, separators=(",", ":")))

            left = asyncio.create_task(client_to_browser())
            right = asyncio.create_task(browser_to_client())
            done, open_tasks = await asyncio.wait(
                {left, right}, return_when=asyncio.FIRST_COMPLETED
            )
            if pending:
                self.cancelled_pending += len(pending)
            for task in open_tasks:
                task.cancel()
            await asyncio.gather(*done, *open_tasks, return_exceptions=True)


@pytest.fixture(scope="module")
def chrome_cdp():
    candidates = [
        shutil.which("google-chrome"), shutil.which("chromium"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    chrome = next((p for p in candidates if p and Path(p).is_file()), None)
    if not chrome:
        pytest.skip("Chrome/Chromium is required for relay conformance")
    profile = tempfile.mkdtemp(prefix="hermes-relay-chrome-")
    proc = subprocess.Popen([
        chrome, "--remote-debugging-port=0", f"--user-data-dir={profile}",
        "--headless=new", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", PAGE_URL,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint_file = Path(profile) / "DevToolsActivePort"
    deadline = time.monotonic() + 15
    ws_url = None
    while time.monotonic() < deadline:
        if endpoint_file.exists():
            lines = endpoint_file.read_text().splitlines()
            if len(lines) >= 2:
                ws_url = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
                break
        time.sleep(.1)
    if not ws_url:
        proc.terminate()
        pytest.fail("Chrome did not expose DevToolsActivePort")
    yield ws_url
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)
    shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture(scope="module")
def relay(chrome_cdp):
    fixture = ProductionOnePageRelay(chrome_cdp)
    fixture.start()
    yield fixture
    fixture.stop()


async def _cdp_call(ws, request_id: int, method: str, params=None) -> dict[str, Any]:
    await ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    async for raw in ws:
        response = json.loads(raw)
        if response.get("id") == request_id:
            return response
    raise RuntimeError("CDP connection closed before the requested response")


def test_real_registry_keeps_all_twelve_schema_bytes_stable(monkeypatch):
    """Capability gating may hide tools, but cannot rewrite model-visible bytes."""
    import tools.browser_tool  # noqa: F401 - registers the ten base tools
    import tools.browser_cdp_tool  # noqa: F401
    import tools.browser_dialog_tool  # noqa: F401
    from tools.registry import registry

    entries = []
    for name in BROWSER_TOOL_NAMES:
        entry = registry.get_entry(name)
        assert entry is not None, f"real browser tool {name} must be registered"
        entries.append(entry)
    before = b"\n".join(_json_bytes(entry.schema) for entry in entries)
    original_checks = [entry.check_fn for entry in entries]
    for entry in entries:
        entry.check_fn = lambda: False
    disabled = b"\n".join(_json_bytes(entry.schema) for entry in entries)
    for entry in entries:
        entry.check_fn = lambda: True
    enabled = b"\n".join(_json_bytes(entry.schema) for entry in entries)
    for entry, check in zip(entries, original_checks):
        entry.check_fn = check

    assert before == disabled == enabled
    assert tuple(entry.schema["name"] for entry in entries) == BROWSER_TOOL_NAMES
    assert len(set(BROWSER_TOOL_NAMES)) == 12


def test_one_page_containment_and_policy_refusals(relay):
    async def exercise():
        async with websockets.connect(relay.url, max_size=None) as ws:
            version = await _cdp_call(ws, 0, "Browser.getVersion")
            assert version["result"] == SYNTHETIC_BROWSER_VERSION
            targets = await _cdp_call(ws, 1, "Target.getTargets")
            infos = targets["result"]["targetInfos"]
            assert [(t["targetId"], t["type"]) for t in infos] == [(SYNTHETIC_TARGET_ID, "page")]
            attached = await _cdp_call(ws, 2, "Target.attachToTarget", {
                "targetId": SYNTHETIC_TARGET_ID, "flatten": True,
            })
            assert attached["result"]["sessionId"]
            attacks = [
                ("Target.attachToTarget", {"targetId": "other-tab", "flatten": True}, "TARGET_NOT_OWNED"),
                ("Target.createTarget", {"url": "about:blank"}, "TARGET_METHOD_BLOCKED"),
                ("Target.activateTarget", {"targetId": SYNTHETIC_TARGET_ID}, "TARGET_METHOD_BLOCKED"),
                ("Target.closeTarget", {"targetId": SYNTHETIC_TARGET_ID}, "TARGET_METHOD_BLOCKED"),
                ("Browser.close", {}, "BROWSER_METHOD_BLOCKED"),
                ("Browser.getWindowForTarget", {"targetId": SYNTHETIC_TARGET_ID}, "BROWSER_METHOD_BLOCKED"),
                ("Browser.grantPermissions", {"permissions": ["geolocation"]}, "BROWSER_METHOD_BLOCKED"),
                ("DOM.setFileInputFiles", {"files": ["/tmp/canary"]}, "UPLOAD_UNSUPPORTED"),
                ("Page.setDownloadBehavior", {"behavior": "allow"}, "DOWNLOAD_BLOCKED"),
            ]
            for request_id, (method, params, category) in enumerate(attacks, 10):
                response = await _cdp_call(ws, request_id, method, params)
                assert category in response["error"]["message"]
    asyncio.run(exercise())
    assert ("synthesize", "Browser.getVersion") in relay.trace
    assert ("send", "Browser.getVersion") not in relay.trace
    assert not [
        event for event in relay.trace
        if event[0] == "synthesize" and event[1] != "Browser.getVersion"
    ]


def test_production_relay_requires_its_unpredictable_path_token(relay):
    async def exercise():
        origin, _token = relay.url.rsplit("/", 1)
        async with websockets.connect(f"{origin}/wrong-token", max_size=None) as ws:
            with pytest.raises(websockets.exceptions.ConnectionClosedError) as closed:
                await ws.recv()
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == 4403

    asyncio.run(exercise())
    assert ("deny", "authentication") in relay.trace


def test_python_cdp_supervisor_is_a_real_consumer(relay):
    supervisor = CDPSupervisor("relay-conformance", relay.url)
    supervisor.start(timeout=10)
    try:
        snapshot = supervisor.snapshot()
        assert snapshot.active
        assert supervisor._page_session_id  # actual attach completed through relay
        assert supervisor._loop is not None
        evaluated = supervisor.evaluate_runtime("document.title")
        assert evaluated["ok"] and evaluated["result"] == "Hermes relay fixture"
        screenshot_future = asyncio.run_coroutine_threadsafe(
            supervisor._cdp(
                "Page.captureScreenshot", {"format": "png"},
                session_id=supervisor._page_session_id,
            ),
            supervisor._loop,
        )
        screenshot = screenshot_future.result(10)
        assert len(base64.b64decode(screenshot["result"]["data"])) > 100

        # Exercise the real supervisor dialog bridge, not a hand-copied parser.
        fired = supervisor.evaluate_runtime("setTimeout(() => alert('relay-dialog'), 10); true")
        assert fired["ok"]
        deadline = time.monotonic() + 5
        while not supervisor.snapshot().pending_dialogs and time.monotonic() < deadline:
            time.sleep(.05)
        dialogs = supervisor.snapshot().pending_dialogs
        assert dialogs and "relay-dialog" in dialogs[0].message
        assert supervisor.respond_to_dialog("dismiss")["ok"]
    finally:
        supervisor.stop()


def _agent_browser_binary(version: str) -> str | None:
    explicit = os.environ.get(_agent_browser_env_name(version))
    strict = os.environ.get("HERMES_REQUIRE_AGENT_BROWSER_PARITY") == "1"
    candidates = [explicit] if strict else [explicit, shutil.which("agent-browser")]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        result = subprocess.run([candidate, "--version"], text=True, capture_output=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip() == f"agent-browser {version}":
            return candidate
    return None


def _agent_browser_env_name(version: str) -> str:
    if version == "0.32.0":
        return "HERMES_INSTALLED_AGENT_BROWSER_0320"
    return "HERMES_AGENT_BROWSER_" + version.replace(".", "")


@pytest.mark.parametrize("version", ["0.26.0", "0.32.0"])
def test_real_agent_browser_consumer_versions(relay, tmp_path, version):
    binary = _agent_browser_binary(version)
    if binary is None:
        unavailable = (
            f"exact agent-browser {version} unavailable; set "
            f"{_agent_browser_env_name(version)} for the E2 parity run"
        )
        if os.environ.get("HERMES_REQUIRE_AGENT_BROWSER_PARITY") == "1":
            pytest.fail(unavailable)
        pytest.skip(unavailable)
    env = os.environ.copy()
    # The daemon appends its own long name and macOS AF_UNIX paths cap at 104
    # bytes, so pytest's intentionally descriptive temp root cannot be used.
    env["AGENT_BROWSER_SOCKET_DIR"] = tempfile.mkdtemp(prefix="ab-e0-", dir="/tmp")
    env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = "5000"
    trace_start = len(relay.trace)

    def run(*args: str, timeout=30, expect_success=True):
        completed = subprocess.run(
            [binary, "--cdp", relay.url, "--json", *args], env=env,
            text=True, capture_output=True, timeout=timeout,
        )
        parsed = json.loads(completed.stdout)
        if expect_success:
            assert completed.returncode == 0, parsed
        return completed.returncode, parsed

    try:
        _, opened = run("open", PAGE_URL)
        assert opened.get("success", True)
        _, snapshot = run("snapshot")
        rendered = json.dumps(snapshot)
        assert "Name" in rendered and "Apply" in rendered
        refs = (snapshot.get("data") or {}).get("refs") or snapshot.get("refs") or {}
        name_ref = next((key for key, value in refs.items() if "Name" in json.dumps(value)), None)
        if name_ref:
            fill_rc, fill_result = run(
                "fill", "@" + name_ref.lstrip("@"), "Hermes", expect_success=False
            )
            assert fill_rc == 0, fill_result
        shot = tmp_path / f"agent-browser-{version}.png"
        _, captured = run("screenshot", str(shot))
        assert captured.get("success", True)
        assert shot.is_file() and shot.stat().st_size > 100
        browser_events = [
            event for event in relay.trace[trace_start:] if event[1].startswith("Browser.")
        ]
        if version == "0.32.0":
            assert ("synthesize", "Browser.getVersion") in browser_events
        assert not [event for event in browser_events if event[0] == "send"]
        assert not [
            event for event in browser_events
            if event[0] == "synthesize" and event[1] != "Browser.getVersion"
        ]
    finally:
        shutil.rmtree(env["AGENT_BROWSER_SOCKET_DIR"], ignore_errors=True)


@pytest.mark.parametrize("version", ["0.26.0", "0.32.0"])
def test_existing_tool_adapter_snapshot_action_and_stale_ref_path(
    relay, monkeypatch, tmp_path, version
):
    """Exercise exact installed consumer refs through the model-visible adapter."""
    binary = _agent_browser_binary(version)
    if binary is None:
        pytest.skip(f"exact agent-browser {version} unavailable")

    from tools import browser_tool

    socket_dir = str(tmp_path / "agent-browser-sockets")
    monkeypatch.setenv("AGENT_BROWSER_SOCKET_DIR", socket_dir)
    monkeypatch.setattr(browser_tool, "_cached_agent_browser", binary)
    monkeypatch.setattr(browser_tool, "_agent_browser_resolved", True)
    task_id = "in-app-adapter-real"
    browser_tool.register_in_app_browser_session(
        task_id=task_id,
        cdp_url=relay.url,
        raw_cdp_url=relay.url,
        profile="test-profile",
        connection_id="test-connection",
        capability_generation=1,
        tab_id="browser:adapter-real",
        binding_generation=1,
        guest_generation="guest-adapter-real",
        task_generation=1,
        snapshot_identity_provider=relay.take_accessibility_snapshots,
    )

    try:
        first = json.loads(browser_tool.browser_snapshot(task_id=task_id))
        assert first["success"] is True
        textbox = re.search(r'textbox "Name" \[ref=(e\d+)\]', first["snapshot"])
        apply = re.search(r'button "Apply" \[ref=(e\d+)\]', first["snapshot"])
        assert textbox and apply

        typed = json.loads(browser_tool.browser_type(textbox.group(1), "Hermes", task_id=task_id))
        clicked = json.loads(browser_tool.browser_click(apply.group(1), task_id=task_id))
        assert typed["success"] is True and clicked["success"] is True

        second = json.loads(browser_tool.browser_snapshot(task_id=task_id))
        assert "Hermes" in second["snapshot"]
        assert textbox.group(1) in re.findall(r"ref=(e\d+)", second["snapshot"])

        unchanged = json.loads(browser_tool.browser_snapshot(task_id=task_id))
        assert re.findall(r"ref=(e\d+)", unchanged["snapshot"]) == re.findall(
            r"ref=(e\d+)", second["snapshot"]
        )

        browser_tool.invalidate_in_app_browser_session_refs(
            task_id=task_id,
            guest_generation="guest-adapter-real",
            task_generation=1,
        )
        stale = json.loads(browser_tool.browser_click(textbox.group(1), task_id=task_id))
        assert stale == {
            "success": False,
            "error": f"STALE_REF: {textbox.group(1)} is stale or unknown",
        }
    finally:
        assert browser_tool.unregister_in_app_browser_session(
            task_id=task_id,
            guest_generation="guest-adapter-real",
            task_generation=1,
        )


def test_cancellation_drops_pending_work(relay):
    async def exercise():
        ws = await websockets.connect(relay.url, max_size=None)
        # A command id is registered as pending before the upstream reply. Closing
        # immediately exercises relay-side cancellation and late-reply suppression.
        await ws.send(json.dumps({
            "id": 9001, "method": "Runtime.evaluate",
            "params": {"expression": "new Promise(r => setTimeout(() => r(1), 10000))", "awaitPromise": True},
        }))
        await ws.close()
    before = relay.cancelled_pending
    asyncio.run(exercise())
    deadline = time.monotonic() + 3
    while relay.cancelled_pending == before and time.monotonic() < deadline:
        time.sleep(.05)
    assert relay.cancelled_pending > before


def test_fifty_mib_boundary_is_prewrite_and_connection_survives(relay):
    assert ProductionOnePageRelay.admit_frame(b"x" * RELAY_MESSAGE_CAP)
    assert not ProductionOnePageRelay.admit_frame(b"x" * (RELAY_MESSAGE_CAP + 1))

    async def exercise():
        async with websockets.connect(relay.url, max_size=None, compression=None) as ws:
            # The over-cap frame is refused by the application without forwarding
            # and without the WebSocket 1009/connection-kill failure mode.
            prefix = b'{"id":7001,"method":"Runtime.evaluate","params":{"pad":"'
            suffix = b'"}}'
            raw = prefix + (b"a" * (RELAY_MESSAGE_CAP + 1 - len(prefix) - len(suffix))) + suffix
            assert len(raw) == RELAY_MESSAGE_CAP + 1
            await ws.send(raw)
            refused = json.loads(await ws.recv())
            assert refused["error"]["code"] == -32050
            healthy = await _cdp_call(ws, 7002, "Target.getTargets")
            assert healthy["result"]["targetInfos"][0]["targetId"] == SYNTHETIC_TARGET_ID
    asyncio.run(exercise())


def test_every_observed_top_level_commit_notifies_ref_tombstone_hook():
    commits = []
    relay = ProductionOnePageRelay(
        "ws://127.0.0.1:1",
        token="A" * 43,
        on_top_level_commit=lambda: commits.append("commit"),
    )

    relay._observe_frame_message(
        {"method": "Page.frameNavigated", "params": {"frame": {"id": "top"}}}
    )
    relay._observe_frame_message(
        {
            "method": "Page.frameNavigated",
            "params": {"frame": {"id": "child", "parentId": "top"}},
        }
    )
    relay._observe_frame_message(
        {"method": "Page.navigatedWithinDocument", "params": {"frameId": "top"}}
    )
    relay._observe_frame_message(
        {"method": "Page.navigatedWithinDocument", "params": {"frameId": "child"}}
    )
    assert commits == ["commit", "commit"]
    relay._loop.close()
