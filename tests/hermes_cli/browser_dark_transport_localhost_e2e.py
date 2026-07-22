#!/usr/bin/env python3
"""Real localhost proof for the existing-origin dark browser transport.

Starts the shipping ``hermes serve`` application on one ephemeral listener,
uses the normal token HTTP authority to mint fresh browser-only tickets, keeps
chat live, and exercises handshake, disable, re-enable, and kill transitions.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request

import psutil
import websockets
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.browser_transport import WIRE_CONTRACT, method_set_hash
from hermes_cli.dashboard_auth.ws_tickets import TTL_SECONDS


def _post(url: str, token: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hermes-Session-Token": token,
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _write_flag(home: Path, enabled: bool) -> None:
    target = home / "config.yaml"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        yaml.safe_dump({"browser": {"in_app": {"enabled": enabled}}}),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _hello(
    connection_id: str,
    *,
    profile: str = "default",
    method_hash: str | None = None,
) -> dict:
    return {
        "type": "client.hello",
        "profile": profile,
        "connection_id": connection_id,
        "browser": {
            "present": True,
            "local_enabled": True,
            "protocol": WIRE_CONTRACT["protocol"],
            "method_set_hash": method_hash or method_set_hash(),
            "methods": [row["name"] for row in WIRE_CONTRACT["required_methods"]],
        },
    }


def _start_server(home: Path, token: str) -> tuple[subprocess.Popen, int]:
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "HERMES_DASHBOARD_SESSION_TOKEN": token,
        "PYTHONPATH": str(ROOT),
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    deadline = time.monotonic() + 30
    lines: list[str] = []
    assert process.stdout is not None
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line:
            lines.append(line.rstrip())
            if line.startswith("HERMES_BACKEND_READY port="):
                return process, int(line.split("=", 1)[1])
        elif process.poll() is not None:
            break
    process.terminate()
    raise RuntimeError("server did not become ready:\n" + "\n".join(lines[-30:]))


async def _upgrade_rejection(url: str) -> str:
    """Require a real network upgrade to fail before yielding an open socket."""

    try:
        socket = await websockets.connect(url, max_size=None)
    except websockets.exceptions.InvalidStatus as exc:
        assert exc.response.status_code in {401, 403}
        return f"http_{exc.response.status_code}"
    try:
        await socket.recv()
    except websockets.exceptions.ConnectionClosed as exc:
        assert exc.code == 4401
        return f"ws_{exc.code}"
    finally:
        await socket.close()
    raise AssertionError("rejected browser credential unexpectedly stayed open")


async def _exercise(port: int, token: str, home: Path, process: subprocess.Popen) -> dict:
    base = f"http://127.0.0.1:{port}"

    async def mint(connection_id: str, profile: str = "default") -> dict:
        return await asyncio.to_thread(
            _post,
            f"{base}/api/auth/browser-ticket",
            token,
            {"profile": profile, "connection_id": connection_id},
        )

    # Mint this first and consume it only after its real 30-second authority
    # expires. Other network cases run while the wall-clock TTL elapses.
    expiring_connection_id = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    expiring = await mint(expiring_connection_id)
    expiry_started = time.monotonic()

    # Prove connection order is independent: browser sends hello first and the
    # gateway holds it only within the shared negotiation deadline until chat's
    # separately authenticated transport joins the exact association tuple.
    order_connection_id = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    order_ticket = await mint(order_connection_id)
    order_browser = await websockets.connect(
        f"ws://127.0.0.1:{port}/api/ws/browser?ticket={order_ticket['ticket']}", max_size=None
    )
    await order_browser.send(json.dumps(_hello(order_connection_id)))
    await asyncio.sleep(0.05)
    order_chat_url = (
        f"ws://127.0.0.1:{port}/api/ws?token={token}"
        f"&connection_id={order_connection_id}&profile=default"
    )
    async with websockets.connect(order_chat_url, max_size=None) as order_chat:
        assert json.loads(await order_chat.recv())["params"]["type"] == "gateway.ready"
        order_ready = json.loads(await asyncio.wait_for(order_browser.recv(), timeout=10))
        assert order_ready["status"] == "ready"
    await order_browser.close()

    connection_id = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    chat_url = (
        f"ws://127.0.0.1:{port}/api/ws?token={token}"
        f"&connection_id={connection_id}&profile=default"
    )
    async with websockets.connect(chat_url, max_size=None) as chat:
        ready = json.loads(await asyncio.wait_for(chat.recv(), timeout=10))
        assert ready["params"]["type"] == "gateway.ready"

        async def dial_browser():
            minted = await mint(connection_id)
            browser_url = f"ws://127.0.0.1:{port}/api/ws/browser?ticket={minted['ticket']}"
            socket = await websockets.connect(browser_url, max_size=None)
            await socket.send(json.dumps(_hello(connection_id)))
            response = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            assert response["status"] == "ready"
            return socket, response

        wrong_profile_ticket = await mint(connection_id)
        wrong_profile_url = (
            f"ws://127.0.0.1:{port}/api/ws/browser?ticket={wrong_profile_ticket['ticket']}"
        )
        wrong_profile_socket = await websockets.connect(wrong_profile_url, max_size=None)
        await wrong_profile_socket.send(json.dumps(_hello(connection_id, profile="gpt")))
        wrong_profile = json.loads(await wrong_profile_socket.recv())
        assert wrong_profile["status"] == "browser_wrong_profile"
        await wrong_profile_socket.wait_closed()

        incompatible_ticket = await mint(connection_id)
        incompatible_url = (
            f"ws://127.0.0.1:{port}/api/ws/browser?ticket={incompatible_ticket['ticket']}"
        )
        incompatible_socket = await websockets.connect(incompatible_url, max_size=None)
        await incompatible_socket.send(json.dumps(_hello(connection_id, method_hash="0" * 64)))
        incompatible = json.loads(await incompatible_socket.recv())
        assert incompatible["status"] == "browser_incompatible"
        await incompatible_socket.wait_closed()
        replay = await _upgrade_rejection(incompatible_url)

        browser, first = await dial_browser()
        _write_flag(home, False)
        disabled = json.loads(await asyncio.wait_for(browser.recv(), timeout=5))
        assert disabled["status"] == "browser_disabled"
        await browser.wait_closed()

        _write_flag(home, True)
        browser, second = await dial_browser()
        killed_response = await asyncio.to_thread(
            _post,
            f"{base}/api/browser/kill",
            token,
            {"profile": "default"},
        )
        assert killed_response["closed"] == 1
        killed = json.loads(await asyncio.wait_for(browser.recv(), timeout=5))
        assert killed["status"] == "browser_killed"
        await browser.wait_closed()

        # Browser teardown does not affect chat: an unknown ordinary RPC still
        # receives its correlated JSON-RPC error response on the original socket.
        await chat.send(json.dumps({"jsonrpc": "2.0", "id": 991, "method": "dark.health", "params": {}}))
        chat_response = json.loads(await asyncio.wait_for(chat.recv(), timeout=10))
        assert chat_response["id"] == 991

        listeners = [
            row
            for row in psutil.Process(process.pid).net_connections(kind="tcp")
            if row.status == psutil.CONN_LISTEN
        ]
        assert len(listeners) == 1, listeners
        assert listeners[0].laddr.port == port

        remaining_ttl = TTL_SECONDS + 1.1 - (time.monotonic() - expiry_started)
        if remaining_ttl > 0:
            await asyncio.sleep(remaining_ttl)
        expired_url = (
            f"ws://127.0.0.1:{port}/api/ws/browser?ticket={expiring['ticket']}"
        )
        expired = await _upgrade_rejection(expired_url)

        return {
            "listener_count": len(listeners),
            "port": port,
            "browser_first": order_ready["status"],
            "ticket_replay": replay,
            "ticket_expiry": expired,
            "wrong_profile": wrong_profile["status"],
            "incompatible": incompatible["status"],
            "first_capability_generation": first["capability_generation"],
            "second_capability_generation": second["capability_generation"],
            "disabled": disabled["status"],
            "killed": killed["status"],
            "chat_after_kill": "healthy",
        }


def main() -> int:
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="hermes-browser-dark-e2e-") as raw_home:
        home = Path(raw_home)
        _write_flag(home, True)
        process, port = _start_server(home, token)
        try:
            result = asyncio.run(_exercise(port, token, home, process))
            print(json.dumps(result, sort_keys=True))
            return 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
