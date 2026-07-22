"""Existing-origin API/WS integration tests for the dark browser route."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import queue
import secrets
import threading
from urllib.parse import urlencode

import pytest
import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli import web_server
from hermes_cli.browser_transport import WIRE_CONTRACT, BrowserTransportManager, method_set_hash
from hermes_cli.config import load_config, save_config
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests, mint_browser_ticket
from tools import browser_tool
from tools.in_app_browser_relay import SYNTHETIC_TARGET_ID


def _connection_id() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _hello(connection_id: str, profile: str = "default", *, method_hash: str | None = None):
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


@pytest.fixture
def browser_client(monkeypatch):
    _reset_for_tests()
    web_server.app.state.browser_transport_manager = BrowserTransportManager()
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)
    monkeypatch.setattr("hermes_cli.mcp_startup.start_background_mcp_discovery", lambda **_kwargs: None)
    config = load_config()
    config.setdefault("browser", {}).setdefault("in_app", {})["enabled"] = True
    save_config(config)
    with TestClient(web_server.app) as client:
        yield client
    _reset_for_tests()


def _mint(client: TestClient, connection_id: str, profile: str = "default") -> str:
    response = client.post(
        "/api/auth/browser-ticket",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        json={"profile": profile, "connection_id": connection_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def _chat_url(connection_id: str, profile: str = "default") -> str:
    return "/api/ws?" + urlencode(
        {"token": web_server._SESSION_TOKEN, "connection_id": connection_id, "profile": profile}
    )


def test_false_default_and_existing_browser_tool_config_are_independent(_isolate_hermes_home):
    config = load_config()
    assert config["browser"]["in_app"]["enabled"] is False
    # Existing browser-tool settings and tool availability are not repurposed.
    assert config["browser"]["inactivity_timeout"] == 120
    assert config["browser"]["engine"] == "auto"


def test_studio_flag_uses_trusted_process_profile_not_an_asserted_sibling(_isolate_hermes_home):
    from hermes_cli.profiles import get_profile_dir

    config = load_config()
    config["browser"]["in_app"]["enabled"] = False
    save_config(config)
    profile_dir = get_profile_dir("gpt")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.yaml").write_text(
        "browser:\n  in_app:\n    enabled: true\n",
        encoding="utf-8",
    )
    web_server._capture_browser_active_profile(web_server.app)
    assert web_server._browser_active_profile(web_server.app) == "default"
    assert web_server._browser_in_app_enabled(web_server.app) is False


def test_real_routes_reject_enabled_sibling_when_selected_default_is_disabled(
    _isolate_hermes_home, monkeypatch
):
    from hermes_cli.profiles import get_profile_dir

    _reset_for_tests()
    web_server.app.state.browser_transport_manager = BrowserTransportManager()
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)
    monkeypatch.setattr("hermes_cli.mcp_startup.start_background_mcp_discovery", lambda **_kwargs: None)
    config = load_config()
    config.setdefault("browser", {}).setdefault("in_app", {})["enabled"] = False
    save_config(config)
    sibling_dir = get_profile_dir("gpt")
    sibling_dir.mkdir(parents=True, exist_ok=True)
    (sibling_dir / "config.yaml").write_text(
        "browser:\n  in_app:\n    enabled: true\n",
        encoding="utf-8",
    )
    connection_id = _connection_id()

    with TestClient(web_server.app) as client:
        wrong = client.post(
            "/api/auth/browser-ticket",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
            json={"profile": "gpt", "connection_id": connection_id},
        )
        assert wrong.status_code == 409
        assert wrong.json()["detail"] == "browser_wrong_profile"

        disabled = client.post(
            "/api/auth/browser-ticket",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
            json={"profile": "default", "connection_id": connection_id},
        )
        assert disabled.status_code == 409
        assert disabled.json()["detail"] == "browser_disabled"

        with pytest.raises(WebSocketDisconnect) as chat_exc:
            with client.websocket_connect(_chat_url(connection_id, "gpt")):
                pass
        assert chat_exc.value.code == 4409
        assert chat_exc.value.reason == "browser_wrong_profile"

        # Even a ticket placed directly in the process-local store cannot use
        # a sibling assertion to select configuration authority at hello time.
        identity = web_server._local_token_identity()
        forged = mint_browser_ticket(
            user_id=identity["user_id"],
            provider=identity["provider"],
            profile="gpt",
            connection_id=connection_id,
        )
        with client.websocket_connect(f"/api/ws/browser?ticket={forged}") as browser:
            browser.send_json(_hello(connection_id, "gpt"))
            assert browser.receive_json()["status"] == "browser_wrong_profile"

    assert web_server.app.state.browser_transport_manager.ready_count == 0


def test_named_process_profile_is_the_positive_route_authority(tmp_path, monkeypatch):
    root = tmp_path / "selected-home"
    selected = root / "profiles" / "gpt"
    selected.mkdir(parents=True)
    (selected / "config.yaml").write_text(
        "browser:\n  in_app:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(selected))
    _reset_for_tests()
    web_server.app.state.browser_transport_manager = BrowserTransportManager()
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda _ws: True)
    monkeypatch.setattr("hermes_cli.mcp_startup.start_background_mcp_discovery", lambda **_kwargs: None)
    connection_id = _connection_id()

    with TestClient(web_server.app) as client:
        assert web_server._browser_active_profile(web_server.app) == "gpt"
        ticket = _mint(client, connection_id, "gpt")
        with client.websocket_connect(_chat_url(connection_id, "gpt")) as chat:
            chat.receive_json()
            with client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
                browser.send_json(_hello(connection_id, "gpt"))
                assert browser.receive_json()["status"] == "ready"


def test_dedicated_route_rejects_long_lived_query_credentials(browser_client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with browser_client.websocket_connect(
            f"/api/ws/browser?token={web_server._SESSION_TOKEN}"
        ):
            pass
    assert exc.value.code == 4401


def test_real_route_hello_hash_mismatch_and_single_use_replay(browser_client):
    connection_id = _connection_id()
    ticket = _mint(browser_client, connection_id)
    with browser_client.websocket_connect(_chat_url(connection_id)) as chat:
        assert chat.receive_json()["params"]["type"] == "gateway.ready"
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
            browser.send_json(_hello(connection_id, method_hash="0" * 64))
            outcome = browser.receive_json()
            assert outcome["status"] == "browser_incompatible"
            assert outcome["local"]["method_set_hash"] == method_set_hash()

    with pytest.raises(WebSocketDisconnect) as exc:
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}"):
            pass
    assert exc.value.code == 4401


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("type",), 1),
        (("profile",), 1),
        (("connection_id",), 1),
        (("browser",), []),
        (("browser", "present"), 1),
        (("browser", "local_enabled"), 1),
        (("browser", "protocol"), []),
        (("browser", "protocol", "major"), True),
        (("browser", "protocol", "major"), "1"),
        (("browser", "protocol", "minor"), True),
        (("browser", "protocol", "minor"), "0"),
        (("browser", "method_set_hash"), 1),
        (("browser", "methods"), "browser.offer"),
        (("browser", "methods"), ["browser.offer", 1]),
    ],
)
def test_real_route_strictly_rejects_every_malformed_hello_field(browser_client, path, value):
    connection_id = _connection_id()
    payload = copy.deepcopy(_hello(connection_id))
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with browser_client.websocket_connect(_chat_url(connection_id)) as chat:
        chat.receive_json()
        ticket = _mint(browser_client, connection_id)
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
            browser.send_json(payload)
            assert browser.receive_json()["status"] != "ready"
    assert web_server.app.state.browser_transport_manager.ready_count == 0


def test_wrong_profile_ticket_is_typed_and_never_ready(browser_client):
    connection_id = _connection_id()
    ticket = _mint(browser_client, connection_id, "default")
    with browser_client.websocket_connect(_chat_url(connection_id, "default")) as chat:
        chat.receive_json()
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
            browser.send_json(_hello(connection_id, "gpt"))
            assert browser.receive_json()["status"] == "browser_wrong_profile"
    assert web_server.app.state.browser_transport_manager.ready_count == 0


def test_credential_derived_wrong_principal_is_typed_and_never_ready(browser_client):
    connection_id = _connection_id()
    # Mint directly from the process-local authority to model a separately
    # authenticated principal. The route derives this principal from the stored
    # provider/user claims; it never accepts a caller-provided principal string.
    ticket = mint_browser_ticket(
        user_id="attacker",
        provider="nous",
        profile="default",
        connection_id=connection_id,
    )
    with browser_client.websocket_connect(_chat_url(connection_id)) as chat:
        chat.receive_json()
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
            browser.send_json(_hello(connection_id))
            assert browser.receive_json()["status"] == "browser_wrong_principal"
    assert web_server.app.state.browser_transport_manager.ready_count == 0


def test_chat_rpc_cannot_advertise_or_acquire_browser_capability(browser_client):
    from tui_gateway.ws import WSTransport

    connection_id = _connection_id()
    with browser_client.websocket_connect(_chat_url(connection_id) + "&browser=true") as chat:
        chat.receive_json()
        manager = web_server.app.state.browser_transport_manager
        assert manager.association_count == 1
        association = manager._chat[0]
        assert isinstance(association.transport, WSTransport)
        assert association.transport.principal == web_server._local_token_identity()["principal"]
        chat.send_json(
            {
                "jsonrpc": "2.0",
                "id": 77,
                "method": "client.hello",
                "params": {"capabilities": {"browser": True}},
            }
        )
        response = chat.receive_json()
        assert response["id"] == 77
        assert "error" in response
        assert web_server.app.state.browser_transport_manager.ready_count == 0


def test_operational_route_rejects_unbound_cdp_frame(browser_client):
    connection_id = _connection_id()
    with browser_client.websocket_connect(_chat_url(connection_id)) as chat:
        chat.receive_json()
        ticket = _mint(browser_client, connection_id)
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
            browser.send_json(_hello(connection_id))
            assert browser.receive_json()["status"] == "ready"
            browser.send_json(
                {
                    "type": "browser.cdp.frame",
                    "sid": "guessed",
                    "profile": "default",
                    "capability_generation": 1,
                    "relay_token": "A" * 43,
                }
            )
            outcome = browser.receive_json()
            assert outcome["status"] == "browser_relay_not_found"
            assert outcome["delivery"] == "not_started"


def test_dedicated_browser_socket_round_trip_is_exact_fenced_and_chat_independent(browser_client):
    connection_id = _connection_id()
    with browser_client.websocket_connect(_chat_url(connection_id)) as chat:
        assert chat.receive_json()["params"]["type"] == "gateway.ready"
        ticket = _mint(browser_client, connection_id)
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
            browser.send_json(_hello(connection_id))
            hello = browser.receive_json()
            assert hello["status"] == "ready"

            manager = web_server.app.state.browser_transport_manager
            context = manager.contexts[0]
            browser.send_json(
                {
                    "type": "client.task.bind",
                    "task_id": "task-operational",
                    "tab_id": "browser:tab-operational",
                    "guest_generation": "guest-generation-1",
                    "task_generation": 7,
                }
            )
            assert browser.receive_json() == {
                "type": "server.task.bound",
                "task_id": "task-operational",
                "tab_id": "browser:tab-operational",
                "guest_generation": "guest-generation-1",
                "task_generation": 7,
            }
            adapter = context.tool_adapters[0]
            relay = adapter.relay
            relay_binding = adapter.binding
            active = browser_tool._active_sessions["task-operational"]
            assert active["cdp_url"] == adapter.cdp_url
            assert active["raw_cdp_url"] == adapter.raw_cdp_url
            assert active["guest_generation"] == "guest-generation-1"
            assert active["task_generation"] == 7
            assert callable(active["_snapshot_identity_provider"])
            completed: queue.Queue[dict | BaseException] = queue.Queue()

            def consume_relay() -> None:
                async def exercise() -> dict:
                    async with websockets.connect(relay.url, max_size=None) as socket:
                        await socket.send(json.dumps({"id": 1, "method": "Target.getTargets", "params": {}}))
                        targets = json.loads(await socket.recv())
                        assert targets["result"]["targetInfos"][0]["targetId"] == SYNTHETIC_TARGET_ID
                        await socket.send(
                            json.dumps(
                                {
                                    "id": 2,
                                    "method": "Target.attachToTarget",
                                    "params": {"targetId": SYNTHETIC_TARGET_ID, "flatten": True},
                                }
                            )
                        )
                        attached = json.loads(await socket.recv())
                        session_id = attached["result"]["sessionId"]
                        await socket.send(
                            json.dumps(
                                {
                                    "id": 3,
                                    "method": "Page.navigate",
                                    "params": {"url": "https://example.test/allowed"},
                                    "sessionId": session_id,
                                }
                            )
                        )
                        response = json.loads(await socket.recv())
                        event = json.loads(await socket.recv())
                        return {"event": event, "response": response, "session_id": session_id}

                try:
                    completed.put(asyncio.run(exercise()))
                except BaseException as exc:  # pragma: no cover - surfaced in the parent assertion
                    completed.put(exc)

            worker = threading.Thread(target=consume_relay, daemon=True)
            worker.start()
            outbound = browser.receive_json()
            assert outbound["type"] == "browser.cdp.send"
            assert outbound["frame"] == {
                "id": 3,
                "method": "Page.navigate",
                "params": {"url": "https://example.test/allowed"},
            }
            assert {
                key: outbound[key]
                for key in ("task_id", "tab_id", "guest_generation", "role", "task_generation")
            } == {
                "task_id": "task-operational",
                "tab_id": "browser:tab-operational",
                "guest_generation": "guest-generation-1",
                "role": "automation",
                "task_generation": 7,
            }

            stale = {**outbound, "type": "browser.cdp.frame", "task_generation": 8}
            stale["frame"] = {"id": 3, "result": {"frameId": "wrong"}}
            browser.send_json(stale)
            assert browser.receive_json()["status"] == "browser_relay_not_found"

            response = {**outbound, "type": "browser.cdp.frame"}
            response["frame"] = {"id": 3, "result": {"frameId": "frame-1"}}
            browser.send_json(response)
            event = {**outbound, "type": "browser.cdp.frame", "operation_id": None}
            event["frame"] = {"method": "Page.loadEventFired", "params": {"timestamp": 1.5}}
            browser.send_json(event)

            result = completed.get(timeout=5)
            if isinstance(result, BaseException):
                raise result
            assert isinstance(result, dict)
            assert result["response"] == {
                "id": 3,
                "result": {"frameId": "frame-1"},
                "sessionId": result["session_id"],
            }
            assert result["event"] == {
                "method": "Page.loadEventFired",
                "params": {"timestamp": 1.5},
                "sessionId": result["session_id"],
            }
            worker.join(timeout=5)
            browser.send_json(
                {
                    "type": "client.task.unbind",
                    "task_id": "task-operational",
                    "tab_id": "browser:tab-operational",
                    "guest_generation": "guest-generation-1",
                    "task_generation": 7,
                }
            )
            assert browser.receive_json()["closed"] is True
            assert "task-operational" not in browser_tool._active_sessions

            # Browser-only teardown cannot close or poison the independent chat leg.
            chat.send_json({"jsonrpc": "2.0", "id": 91, "method": "health", "params": {}})
            assert chat.receive_json()["id"] == 91


def test_browser_socket_fairly_interleaves_busy_sibling_relays(browser_client):
    connection_id = _connection_id()
    with browser_client.websocket_connect(_chat_url(connection_id)) as chat:
        chat.receive_json()
        ticket = _mint(browser_client, connection_id)
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
            browser.send_json(_hello(connection_id))
            assert browser.receive_json()["status"] == "ready"

            for suffix in ("a", "b"):
                browser.send_json(
                    {
                        "type": "client.task.bind",
                        "task_id": f"task-{suffix}",
                        "tab_id": f"tab-{suffix}",
                        "guest_generation": f"guest-{suffix}",
                        "task_generation": 1,
                    }
                )
                assert browser.receive_json()["type"] == "server.task.bound"

            manager = web_server.app.state.browser_transport_manager
            context = manager.contexts[0]
            first, second = context.tool_adapters
            for sequence in (1, 2):
                manager.admit_operation(
                    first.binding,
                    {"id": sequence, "method": "Page.getFrameTree", "params": {}},
                )
                manager.admit_operation(
                    second.binding,
                    {"id": sequence, "method": "Page.getFrameTree", "params": {}},
                )

            outbound = [browser.receive_json() for _ in range(4)]
            assert [row["task_id"] for row in outbound] == [
                "task-a",
                "task-b",
                "task-a",
                "task-b",
            ]
            assert [row["frame"]["id"] for row in outbound] == [1, 1, 2, 2]


def test_overlapping_successor_bind_collision_is_task_scoped(browser_client):
    connection_id = _connection_id()
    with browser_client.websocket_connect(_chat_url(connection_id)) as chat:
        chat.receive_json()
        predecessor_ticket = _mint(browser_client, connection_id)
        with browser_client.websocket_connect(
            f"/api/ws/browser?ticket={predecessor_ticket}"
        ) as predecessor:
            predecessor.send_json(_hello(connection_id))
            assert predecessor.receive_json()["status"] == "ready"
            predecessor.send_json(
                {
                    "type": "client.task.bind",
                    "task_id": "task-overlap",
                    "tab_id": "tab-predecessor",
                    "guest_generation": "guest-predecessor",
                    "task_generation": 1,
                }
            )
            assert predecessor.receive_json()["type"] == "server.task.bound"

            successor_ticket = _mint(browser_client, connection_id)
            with browser_client.websocket_connect(
                f"/api/ws/browser?ticket={successor_ticket}"
            ) as successor:
                successor.send_json(_hello(connection_id))
                assert successor.receive_json()["status"] == "ready"
                successor.send_json(
                    {
                        "type": "client.task.bind",
                        "task_id": "task-overlap",
                        "tab_id": "tab-successor",
                        "guest_generation": "guest-successor",
                        "task_generation": 2,
                    }
                )
                assert successor.receive_json() == {
                    "type": "server.task.bound",
                    "task_id": "task-overlap",
                    "tab_id": "tab-successor",
                    "guest_generation": "guest-successor",
                    "task_generation": 2,
                }
                assert browser_tool._active_sessions["task-overlap"]["connection_id"] == connection_id
                assert browser_tool._active_sessions["task-overlap"]["guest_generation"] == "guest-successor"

                # A distinct sibling proves preemption was task-scoped and did
                # not tear down the successor socket or its browser context.
                successor.send_json(
                    {
                        "type": "client.task.bind",
                        "task_id": "task-successor-sibling",
                        "tab_id": "tab-successor-sibling",
                        "guest_generation": "guest-successor-sibling",
                        "task_generation": 1,
                    }
                )
                assert successor.receive_json() == {
                    "type": "server.task.bound",
                    "task_id": "task-successor-sibling",
                    "tab_id": "tab-successor-sibling",
                    "guest_generation": "guest-successor-sibling",
                    "task_generation": 1,
                }


def test_disable_mid_connection_and_browser_kill_leave_chat_healthy(browser_client):
    connection_id = _connection_id()
    with browser_client.websocket_connect(_chat_url(connection_id)) as chat:
        assert chat.receive_json()["params"]["type"] == "gateway.ready"

        ticket = _mint(browser_client, connection_id)
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
            browser.send_json(_hello(connection_id))
            assert browser.receive_json()["status"] == "ready"

            config = load_config()
            config["browser"]["in_app"]["enabled"] = False
            save_config(config)
            assert browser.receive_json()["status"] == "browser_disabled"

        # Chat stays accepted and dispatches an ordinary health-shaped RPC.
        chat.send_json({"jsonrpc": "2.0", "id": 1, "method": "health", "params": {}})
        response = chat.receive_json()
        assert response["id"] == 1

        # Re-enable, reconnect browser, then invoke the authenticated browser-only kill.
        config = load_config()
        config["browser"]["in_app"]["enabled"] = True
        save_config(config)
        ticket = _mint(browser_client, connection_id)
        with browser_client.websocket_connect(f"/api/ws/browser?ticket={ticket}") as browser:
            browser.send_json(_hello(connection_id))
            assert browser.receive_json()["status"] == "ready"
            killed = browser_client.post(
                "/api/browser/kill",
                headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
                json={"profile": "default"},
            )
            assert killed.json()["closed"] == 1
            assert browser.receive_json()["status"] == "browser_killed"

        chat.send_json({"jsonrpc": "2.0", "id": 2, "method": "health", "params": {}})
        assert chat.receive_json()["id"] == 2


def test_only_one_browser_ws_route_and_no_second_server_constructed():
    routes = [getattr(route, "path", None) for route in web_server.app.router.routes]
    assert routes.count("/api/ws/browser") == 1
    # The implementation is mounted on the same FastAPI object as chat.
    assert routes.count("/api/ws") == 1
