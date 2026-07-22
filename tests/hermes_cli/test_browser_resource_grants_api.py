from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import asyncio
import threading

from fastapi.testclient import TestClient
import pytest

from hermes_cli.browser_resource_grants import BrowserResourceGrantAuthority
from hermes_cli.browser_upload_sources import BrowserUploadSourceAuthority


class _PreviewHandler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        type(self).requests.append((self.path, dict(self.headers)))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/app/index.html")
            self.send_header("Set-Cookie", "studio=must-not-leak")
            self.end_headers()
            return
        body = f"preview:{self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "studio=must-not-leak")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def preview_server():
    _PreviewHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PreviewHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def grant_client(monkeypatch, tmp_path):
    from hermes_cli import browser_transport, web_server

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = BrowserResourceGrantAuthority(
        workspace_root=lambda profile, session: (
            workspace if (profile, session) == ("coding", "session-1") else None
        )
    )
    upload_authority = BrowserUploadSourceAuthority(
        workspace_root=lambda profile, session: (
            workspace if (profile, session) == ("coding", "session-1") else None
        )
    )

    class UploadLease:
        def validate_upload_scope(self, **scope):
            from hermes_cli.browser_transport import BrowserProtocolError

            expected = _upload_scope()
            assert scope["profile"] == "coding"
            assert scope["principal"].startswith("dashboard-token:")
            for key in (
                "connection_id", "transport_id", "browser_sid", "capability_generation",
                "binding_generation", "task_id", "task_generation", "tab_id",
                "source_session_id",
            ):
                if scope[key] != expected[key]:
                    raise BrowserProtocolError(
                        "browser_task_not_bound", "upload task is not bound to this session"
                    )

    monkeypatch.setattr(browser_transport, "get_browser_transport_manager", lambda _app: UploadLease())

    web_server.app.state.browser_resource_grants = authority
    web_server.app.state.browser_upload_sources = upload_authority
    with TestClient(web_server.app) as client:
        yield client, workspace, authority, web_server


def _mint_scope():
    return {
        "connection_id": "desktop-connection-1",
        "tab_id": "browser:tab-1",
        "guest_generation": "guest-generation-1",
        "source_session_id": "session-1",
    }


def _upload_scope():
    return {
        "connection_id": "desktop-connection-1",
        "transport_id": "transport-1",
        "browser_sid": "browser-sid-1",
        "capability_generation": "capability-1",
        "task_id": "task-1",
        "task_generation": "1",
        "tab_id": "browser:tab-1",
        "tab_incarnation": "tab-incarnation-1",
        "binding_generation": "binding-1",
        "document_generation": "document-1",
        "frame_id": "frame-1",
        "origin": "https://example.test",
        "chooser_id": "chooser-1",
        "backend_node_id": "node-1",
        "form_fingerprint": "form-1",
        "chooser_mode": "selectSingle",
        "source_session_id": "session-1",
    }


def _upload_delivery_headers(web_server, grant):
    scope = _upload_scope()
    header_names = {
        "connection_id": "Connection",
        "transport_id": "Transport",
        "browser_sid": "Sid",
        "capability_generation": "Capability-Generation",
        "task_id": "Task",
        "task_generation": "Task-Generation",
        "tab_id": "Tab",
        "tab_incarnation": "Tab-Incarnation",
        "binding_generation": "Binding-Generation",
        "document_generation": "Document-Generation",
        "frame_id": "Frame",
        "origin": "Origin",
        "chooser_id": "Chooser",
        "backend_node_id": "Backend-Node",
        "form_fingerprint": "Form-Fingerprint",
        "chooser_mode": "Chooser-Mode",
        "source_session_id": "Source-Session",
    }
    headers = {
        "X-Hermes-Session-Token": web_server._SESSION_TOKEN,
        "X-Hermes-Browser-Grant": grant["deliveryCredential"],
        "X-Hermes-Browser-Source-Record-Revision": grant["sourceRecordRevision"],
        "X-Hermes-Browser-Profile": "coding",
    }
    headers.update({f"X-Hermes-Browser-{header_names[key]}": value for key, value in scope.items()})
    return headers


def _delivery_headers(web_server, grant, **overrides):
    values = {
        "X-Hermes-Browser-Grant": grant["deliveryCredential"],
        "X-Hermes-Browser-Recipient": web_server._local_token_identity()["principal"],
        "X-Hermes-Browser-Profile": "coding",
        "X-Hermes-Browser-Connection": "desktop-connection-1",
        "X-Hermes-Browser-Tab": "browser:tab-1",
        "X-Hermes-Browser-Generation": "guest-generation-1",
        "X-Hermes-Browser-Source-Session": "session-1",
    }
    values.update(overrides)
    return values


def test_artifact_mint_is_authenticated_and_delivery_needs_only_exact_scoped_grant(grant_client):
    client, workspace, _authority, web_server = grant_client
    artifact = workspace / "report.pdf"
    artifact.write_bytes(b"0123456789")
    payload = {
        "profile": "coding",
        "path": str(artifact),
        "scope": _mint_scope(),
        "ttl_seconds": 120,
    }

    assert client.post("/api/browser/grants/artifact", json=payload).status_code == 401
    minted_response = client.post(
        "/api/browser/grants/artifact",
        json=payload,
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    assert minted_response.status_code == 201
    grant = minted_response.json()
    serialized = minted_response.text
    assert str(artifact) not in serialized
    assert "0123456789" not in serialized
    assert grant["opaqueRef"] != grant["deliveryCredential"]
    assert grant["recipient"].startswith("dashboard-token:")

    delivery = client.get(
        f"/api/browser/artifacts/{grant['opaqueRef']}",
        headers={**_delivery_headers(web_server, grant), "Range": "bytes=2-5"},
    )
    assert delivery.status_code == 206
    assert delivery.content == b"2345"
    assert delivery.headers["content-range"] == "bytes 2-5/10"
    assert delivery.headers["x-content-type-options"] == "nosniff"
    assert delivery.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in delivery.headers


def test_cross_tab_delivery_revokes_valid_grant_without_profile_or_path_oracle(grant_client):
    client, workspace, _authority, web_server = grant_client
    artifact = workspace / "page.html"
    artifact.write_text("<h1>hostile</h1>")
    minted = client.post(
        "/api/browser/grants/artifact",
        json={"profile": "coding", "path": str(artifact), "scope": _mint_scope()},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    ).json()
    url = f"/api/browser/artifacts/{minted['opaqueRef']}"

    wrong = client.get(
        url,
        headers=_delivery_headers(web_server, minted, **{"X-Hermes-Browser-Tab": "browser:tab-2"}),
    )
    stale = client.get(url, headers=_delivery_headers(web_server, minted))

    assert wrong.status_code == 401
    assert stale.status_code == 401
    assert str(artifact) not in wrong.text + stale.text
    assert "hostile" not in wrong.text + stale.text


def test_preview_proxy_uses_existing_listener_strips_cookies_and_gateway_credentials(
    grant_client, preview_server
):
    client, _workspace, _authority, web_server = grant_client
    upstream = f"http://127.0.0.1:{preview_server.server_port}/app/index.html"
    minted_response = client.post(
        "/api/browser/grants/preview",
        json={"profile": "coding", "upstream_url": upstream, "scope": _mint_scope()},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    assert minted_response.status_code == 201
    grant = minted_response.json()
    assert upstream not in minted_response.text
    headers = _delivery_headers(web_server, grant)
    headers.update(
        {
            "Authorization": "Bearer gateway-secret-must-not-forward",
            "Cookie": "gateway=cookie-must-not-forward",
            "X-Hermes-Session-Token": "gateway-session-must-not-forward",
        }
    )

    proxied = client.get(grant["proxyPath"], headers=headers)

    assert proxied.status_code == 200
    assert proxied.content == b"preview:/app/index.html"
    assert proxied.headers["x-content-type-options"] == "nosniff"
    assert proxied.headers["referrer-policy"] == "no-referrer"
    assert proxied.headers["cross-origin-opener-policy"] == "same-origin"
    assert proxied.headers["content-security-policy"] == "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    assert "set-cookie" not in proxied.headers
    upstream_path, upstream_headers = _PreviewHandler.requests[-1]
    assert upstream_path == "/app/index.html"
    assert "Authorization" not in upstream_headers
    assert "Cookie" not in upstream_headers
    assert "X-Hermes-Session-Token" not in upstream_headers
    assert "X-Hermes-Browser-Grant" not in upstream_headers


def test_preview_redirect_is_confined_and_rewritten_without_upstream_origin(grant_client, preview_server):
    client, _workspace, _authority, web_server = grant_client
    upstream = f"http://127.0.0.1:{preview_server.server_port}/redirect"
    grant = client.post(
        "/api/browser/grants/preview",
        json={"profile": "coding", "upstream_url": upstream, "scope": _mint_scope()},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    ).json()

    response = client.get(grant["proxyPath"], headers=_delivery_headers(web_server, grant), follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == f"/api/browser/preview/{grant['opaqueRef']}/app/index.html"
    assert f"127.0.0.1:{preview_server.server_port}" not in response.headers["location"]
    assert "set-cookie" not in response.headers


def test_revoke_endpoint_is_exact_scope_and_profile_bound(grant_client, preview_server):
    client, _workspace, _authority, web_server = grant_client
    grant = client.post(
        "/api/browser/grants/preview",
        json={
            "profile": "coding",
            "upstream_url": f"http://127.0.0.1:{preview_server.server_port}/",
            "scope": _mint_scope(),
        },
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    ).json()
    revoked = client.post(
        "/api/browser/grants/revoke",
        json={"profile": "coding", "scope": _mint_scope()},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    after = client.get(grant["proxyPath"], headers=_delivery_headers(web_server, grant))

    assert revoked.status_code == 200
    assert revoked.json() == {"ok": True, "revoked": 1}
    assert after.status_code == 401


def test_workspace_root_comes_from_each_requested_profile_session_database(
    monkeypatch, tmp_path
):
    from hermes_cli import web_server
    from hermes_state import SessionDB

    homes = {profile: tmp_path / profile for profile in ("coding", "other")}
    workspaces = {profile: tmp_path / f"workspace-{profile}" for profile in homes}
    for profile, home in homes.items():
        home.mkdir()
        workspaces[profile].mkdir()
        db = SessionDB(db_path=home / "state.db")
        try:
            db.create_session("session-1", "desktop", cwd=str(workspaces[profile]))
        finally:
            db.close()
    monkeypatch.setattr(web_server, "_browser_profile_home", homes.__getitem__)

    assert (
        web_server._browser_workspace_root(web_server.app, "coding", "session-1")
        == workspaces["coding"]
    )
    assert (
        web_server._browser_workspace_root(web_server.app, "other", "session-1")
        == workspaces["other"]
    )
    assert web_server._browser_workspace_root(web_server.app, "coding", "missing") is None


def test_preview_websocket_relays_frames_with_no_gateway_cookie_or_bearer_forwarding(
    monkeypatch, grant_client, preview_server
):
    import websockets

    client, _workspace, _authority, web_server = grant_client
    grant = client.post(
        "/api/browser/grants/preview",
        json={
            "profile": "coding",
            "upstream_url": f"http://127.0.0.1:{preview_server.server_port}/socket",
            "scope": _mint_scope(),
        },
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    ).json()
    calls = []

    class FakeUpstream:
        subprotocol = None

        def __init__(self):
            self.queue = asyncio.Queue()

        def __aiter__(self):
            return self

        async def __anext__(self):
            value = await self.queue.get()
            if value is None:
                raise StopAsyncIteration
            return value

        async def send(self, payload):
            await self.queue.put(f"echo:{payload}")
            await self.queue.put(None)

        async def close(self):
            await self.queue.put(None)

    async def connect(target, **kwargs):
        calls.append((target, kwargs))
        return FakeUpstream()

    monkeypatch.setattr(websockets, "connect", connect)
    headers = _delivery_headers(web_server, grant)
    headers.update({"Authorization": "Bearer gateway-secret", "Cookie": "gateway=secret"})

    with client.websocket_connect(grant["proxyPath"], headers=headers) as socket:
        socket.send_text("hello")
        assert socket.receive_text() == "echo:hello"

    assert calls[0][0] == f"ws://127.0.0.1:{preview_server.server_port}/socket"
    assert set(calls[0][1]) == {
        "close_timeout",
        "compression",
        "max_size",
        "open_timeout",
        "origin",
        "subprotocols",
    }


def test_upload_source_requires_normal_auth_and_exact_one_use_tuple(grant_client):
    client, workspace, _authority, web_server = grant_client
    payload = b"opaque upload bytes"
    source = workspace / "upload.txt"
    source.write_bytes(payload)
    body = {"profile": "coding", "scope": _upload_scope()}

    assert client.post("/api/browser/upload-sources/candidates", json=body).status_code == 401
    candidates = client.post(
        "/api/browser/upload-sources/candidates",
        json=body,
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    assert candidates.status_code == 200
    assert str(source) not in candidates.text
    candidate = candidates.json()["candidates"][0]
    grant_response = client.post(
        "/api/browser/upload-sources/grant",
        json={
            **body,
            "candidate_id": candidate["candidateId"],
            "source_record_revision": candidate["sourceRecordRevision"],
        },
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    assert grant_response.status_code == 201
    grant = grant_response.json()
    assert grant["sourceRecordRevision"] == candidate["sourceRecordRevision"]
    assert str(source) not in grant_response.text
    assert payload.decode() not in grant_response.text

    url = f"/api/browser/upload-sources/{grant['opaqueRef']}"
    missing_normal_auth = {
        key: value
        for key, value in _upload_delivery_headers(web_server, grant).items()
        if key != "X-Hermes-Session-Token"
    }
    assert client.get(url, headers=missing_normal_auth).status_code == 401
    delivered = client.get(url, headers=_upload_delivery_headers(web_server, grant))
    replay = client.get(url, headers=_upload_delivery_headers(web_server, grant))

    assert delivered.status_code == 200
    assert delivered.content == payload
    assert delivered.headers["content-length"] == str(len(payload))
    assert delivered.headers["x-hermes-upload-sha256"] == grant["sha256"]
    assert replay.status_code == 404


def test_upload_source_rejects_foreign_source_session_before_workspace_lookup(grant_client):
    client, workspace, _authority, web_server = grant_client
    (workspace / "private.txt").write_bytes(b"must not enumerate")
    scope = {**_upload_scope(), "source_session_id": "foreign-session"}

    response = client.post(
        "/api/browser/upload-sources/candidates",
        json={"profile": "coding", "scope": scope},
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "browser_task_not_bound"
    assert "private.txt" not in response.text


def test_upload_source_revoke_is_authenticated_and_exact_tuple_bound(grant_client):
    client, workspace, _authority, web_server = grant_client
    (workspace / "revoke.txt").write_bytes(b"revoke me")
    body = {"profile": "coding", "scope": _upload_scope()}
    auth = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}
    candidate = client.post(
        "/api/browser/upload-sources/candidates", json=body, headers=auth
    ).json()["candidates"][0]
    grant = client.post(
        "/api/browser/upload-sources/grant",
        json={
            **body,
            "candidate_id": candidate["candidateId"],
            "source_record_revision": candidate["sourceRecordRevision"],
        },
        headers=auth,
    ).json()
    revoke_body = {**body, "opaque_refs": [grant["opaqueRef"]]}
    assert client.post("/api/browser/upload-sources/revoke", json=revoke_body).status_code == 401
    wrong = {**revoke_body, "scope": {**_upload_scope(), "chooser_id": "wrong-chooser"}}
    assert client.post("/api/browser/upload-sources/revoke", json=wrong, headers=auth).json()["revoked"] == 0
    assert client.post("/api/browser/upload-sources/revoke", json=revoke_body, headers=auth).json() == {
        "ok": True,
        "revoked": 1,
    }
    assert client.get(
        f"/api/browser/upload-sources/{grant['opaqueRef']}",
        headers=_upload_delivery_headers(web_server, grant),
    ).status_code == 404
