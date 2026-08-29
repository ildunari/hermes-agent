"""Regression harness for the dashboard auth gate.

Phase 0 — establish a baseline pin on the current (pre-OAuth) behavior so
later phases can prove they didn't break loopback mode.
"""
import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

# Phase 5 / Phase 6: these tests mutate ``web_server.app.state.auth_required``
# at module level. Run them in the same xdist worker so they don't race
# against each other (and against any other file that also touches
# ``app.state``) — the marker name is shared across all dashboard-auth test
# files that gate the app.
from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def client_loopback():
    # Pin the bound-host state for host_header_middleware so requests with
    # default Host: testclient pass the DNS-rebinding check.  TestClient
    # sends Host: testserver by default, but our middleware accepts the
    # loopback aliases when bound_host is loopback.
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_allowed_hosts = getattr(web_server.app.state, "trusted_public_hosts", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.trusted_public_hosts = frozenset()
    client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    yield client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.trusted_public_hosts = prev_allowed_hosts






def test_loopback_configured_allowed_host_is_accepted(client_loopback):
    """Trusted reverse-proxy hostnames can be allowlisted exactly."""
    web_server.app.state.trusted_public_hosts = frozenset({
        "macstudio.tailf7342a.ts.net",
        "100.69.228.58",
    })
    r = client_loopback.get(
        "/api/status",
        headers={"Host": "macstudio.tailf7342a.ts.net:9119"},
    )
    assert r.status_code == 200

    r = client_loopback.get("/api/status", headers={"Host": "100.69.228.58:9119"})
    assert r.status_code == 200

    r = client_loopback.get("/api/status", headers={"Host": "evil.test"})
    assert r.status_code == 400


def test_dashboard_allowed_hosts_normalize_full_urls(monkeypatch):
    """Operators may paste full Tailscale URLs; only the host is trusted."""
    monkeypatch.setattr(
        web_server,
        "load_config",
        lambda: {
            "dashboard": {
                "allowed_hosts": [
                    "https://macstudio.tailf7342a.ts.net:9119/",
                    "100.69.228.58:9119",
                    "*",
                ],
                "public_url": "https://hermes.example.test/webui",
            }
        },
    )
    assert web_server._configured_dashboard_allowed_hosts() == frozenset({
        "macstudio.tailf7342a.ts.net",
        "100.69.228.58",
    })


def test_dashboard_allowed_hosts_accept_config_set_json_string(monkeypatch):
    """`hermes config set` stores list-looking values as strings today."""
    monkeypatch.setattr(
        web_server,
        "load_config",
        lambda: {
            "dashboard": {
                "allowed_hosts": '["macstudio.tailf7342a.ts.net", "100.69.228.58", "fd7a:115c:a1e0::ba35:e43a"]'
            }
        },
    )
    assert web_server._configured_dashboard_allowed_hosts() == frozenset({
        "macstudio.tailf7342a.ts.net",
        "100.69.228.58",
        "fd7a:115c:a1e0::ba35:e43a",
    })


def test_websocket_host_origin_uses_configured_allowed_hosts():
    """WebSocket upgrades need the same allowlist as HTTP middleware."""
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_allowed_hosts = getattr(web_server.app.state, "trusted_public_hosts", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.trusted_public_hosts = frozenset({"macstudio.tailf7342a.ts.net"})
    try:
        ws = SimpleNamespace(headers={
            "host": "macstudio.tailf7342a.ts.net:9119",
            "origin": "https://macstudio.tailf7342a.ts.net:9119",
        })
        assert web_server._ws_host_origin_reason(cast(Any, ws)) is None

        ws = SimpleNamespace(headers={
            "host": "macstudio.tailf7342a.ts.net:9119",
            "origin": "https://evil.test",
        })
        reason = web_server._ws_host_origin_reason(cast(Any, ws))
        assert reason and reason.startswith("origin_mismatch")

        ws = SimpleNamespace(headers={"host": "evil.test"})
        reason = web_server._ws_host_origin_reason(cast(Any, ws))
        assert reason and reason.startswith("host_mismatch")
    finally:
        web_server.app.state.bound_host = prev_host
        web_server.app.state.trusted_public_hosts = prev_allowed_hosts


# ---------------------------------------------------------------------------
# should_require_auth predicate (Task 0.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host,allow_public,expected", [
    ("127.0.0.1", False, False),
    ("127.0.0.1", True,  False),
    ("localhost", False, False),
    ("::1",       False, False),
    # --insecure (allow_public=True) NO LONGER bypasses the gate on a public
    # bind (June 2026 hermes-0day hardening). Non-loopback always requires auth.
    ("0.0.0.0",   True,  True),
    ("0.0.0.0",   False, True),
    ("192.168.1.5", False, True),
    ("10.0.0.1",  True,  True),     # allow_public ignored — LAN IP is public
    ("100.64.0.1", False, True),    # Tailscale CGNAT — treated as public
    ("hermes-agent-prod-abc.fly.dev", False, True),
])
def test_should_require_auth_truth_table(host, allow_public, expected):
    from hermes_cli.web_server import should_require_auth
    assert should_require_auth(host, allow_public) is expected


def test_empty_provider_login_page_shows_supported_auth_paths():
    from hermes_cli.dashboard_auth import clear_providers
    from hermes_cli.dashboard_auth.login_page import render_login_html

    clear_providers()
    html = render_login_html()

    assert "--insecure" not in html
    assert "username/password provider" in html
    assert "OAuth provider" in html
    assert "127.0.0.1" in html
    assert "SSH tunnel" in html
    assert "Tailscale" in html
    assert (
        'href="https://hermes-agent.nousresearch.com/docs/'
        'user-guide/features/web-dashboard#authentication-gated-mode"'
    ) in html


# ---------------------------------------------------------------------------
# start_server stashes auth_required on app.state (Task 0.3)
# ---------------------------------------------------------------------------


def _stub_uvicorn_run(monkeypatch):
    """Replace uvicorn.Config/Server with no-op fakes so start_server
    returns immediately (rather than blocking on the event loop). Returns the dict
    that will capture the keyword args.
    """
    import asyncio
    import contextlib
    import uvicorn
    captured: dict = {"kwargs": {}}

    class _FakeConfig:
        loaded = True
        host = "127.0.0.1"
        port = 8000

        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

        def load(self):
            pass

        class lifespan_class:
            should_exit = False
            state: dict = {}

            def __init__(self, *a, **kw):
                pass

            async def startup(self):
                pass

            async def shutdown(self):
                pass

    class _FakeServer:
        should_exit = False
        started = True
        servers: list = []
        lifespan = None

        @staticmethod
        def capture_signals():
            return contextlib.nullcontext()

        async def startup(self, sockets=None):
            pass

        async def main_loop(self):
            pass

        async def shutdown(self, sockets=None):
            pass

    monkeypatch.setattr(uvicorn, "Config", _FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", lambda config: _FakeServer())
    return captured


def _restore_app_state_after_test(monkeypatch, *names):
    """Restore app.state attributes after start_server mutates them."""
    for name in names:
        monkeypatch.setattr(
            web_server.app.state,
            name,
            getattr(web_server.app.state, name, None),
            raising=False,
        )


def test_start_server_loopback_sets_auth_required_false(monkeypatch):
    """Loopback bind: app.state.auth_required is False after start_server."""
    _stub_uvicorn_run(monkeypatch)
    # Force a fresh state to detect that start_server actually set it.
    web_server.app.state.auth_required = None
    web_server.start_server(
        host="127.0.0.1", port=9119,
        open_browser=False, allow_public=False,
    )
    assert web_server.app.state.auth_required is False


def test_start_server_insecure_public_no_longer_bypasses_gate(monkeypatch):
    """``--insecure`` (allow_public=True) on a public host: gate now ENGAGES.

    June 2026 hardening: --insecure no longer disables auth. With no providers
    registered, the bind fails closed (SystemExit) and auth_required is True.
    """
    from hermes_cli.dashboard_auth import clear_providers
    clear_providers()
    _stub_uvicorn_run(monkeypatch)
    web_server.app.state.auth_required = None
    with pytest.raises(SystemExit):
        web_server.start_server(
            host="0.0.0.0", port=9119,
            open_browser=False, allow_public=True,
        )
    assert web_server.app.state.auth_required is True


def test_start_server_public_without_insecure_records_auth_required(monkeypatch):
    """Public bind without --insecure: the gate engages and auth_required=True.

    With no providers registered, this fails closed with SystemExit. The
    flag-stashing happens BEFORE the exit so the rest of the system can
    branch on it. (See task 3.5 tests below for the with-provider path.)
    """
    from hermes_cli.dashboard_auth import clear_providers
    clear_providers()
    _stub_uvicorn_run(monkeypatch)
    web_server.app.state.auth_required = None
    with pytest.raises(SystemExit):
        web_server.start_server(
            host="0.0.0.0", port=9119,
            open_browser=False, allow_public=False,
        )
    assert web_server.app.state.auth_required is True


# ---------------------------------------------------------------------------
# Task 3.5: start_server fail-closed + proxy_headers + index-token suppression
# ---------------------------------------------------------------------------


def test_start_server_gate_with_provider_proceeds_and_sets_proxy_headers(monkeypatch):
    """With at least one provider, public bind + no --insecure starts the server.

    The SystemExit-refusing-to-bind guard is REPLACED in gated mode by
    "the gate engages", so as long as a provider is registered the bind
    succeeds.  uvicorn is called with proxy_headers=True so X-Forwarded-Proto
    from Fly's TLS terminator is honoured for cookie Secure-flag decisions.
    """
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

    clear_providers()
    register_provider(StubAuthProvider())
    captured = _stub_uvicorn_run(monkeypatch)
    try:
        web_server.app.state.auth_required = None
        web_server.start_server(
            host="0.0.0.0", port=9119,
            open_browser=False, allow_public=False,
        )
        assert web_server.app.state.auth_required is True
        assert captured["kwargs"].get("host") == "0.0.0.0"
        assert captured["kwargs"].get("proxy_headers") is True
        assert captured["kwargs"].get("forwarded_allow_ips") == [
            "127.0.0.1",
            "::1",
        ]
    finally:
        clear_providers()


def test_start_server_passes_bounded_trusted_proxy_networks(monkeypatch, caplog):
    """A configured proxy network reaches uvicorn without broadening to all peers."""
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

    clear_providers()
    register_provider(StubAuthProvider())
    captured = _stub_uvicorn_run(monkeypatch)
    monkeypatch.setattr(
        web_server,
        "load_config",
        lambda: {"dashboard": {"trusted_proxies": ["172.18.0.23/16"]}},
    )
    try:
        with caplog.at_level(logging.INFO, logger=web_server._log.name):
            web_server.start_server(
                host="0.0.0.0", port=9119,
                open_browser=False, allow_public=False,
            )
        assert captured["kwargs"]["forwarded_allow_ips"] == [
            "127.0.0.1",
            "::1",
            "172.18.0.0/16",
        ]
        assert (
            "Dashboard trusted proxies: 127.0.0.1, ::1, 172.18.0.0/16"
            in caplog.text
        )
    finally:
        clear_providers()


def test_trusted_proxy_allowlist_rejects_unbounded_entries(caplog):
    """Wildcard and whole-address-space trust must fail closed."""
    trusted = web_server._dashboard_forwarded_allow_ips({
        "trusted_proxies": ["*", "0.0.0.0/0", "::/0", "172.18.0.7"],
    })

    assert trusted == ["127.0.0.1", "::1", "172.18.0.7"]
    assert "never '*' or a /0 network" in caplog.text


def test_trusted_container_proxy_controls_https_detection():
    """Only a configured bridge peer may turn X-Forwarded-Proto into HTTPS."""
    from hermes_cli.dashboard_auth.cookies import detect_https
    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    trusted = web_server._dashboard_forwarded_allow_ips({
        "trusted_proxies": ["172.18.0.0/16"],
    })

    async def detected_scheme(peer: str) -> bool:
        observed: dict[str, bool] = {}

        async def downstream(scope, receive, send):
            observed["https"] = detect_https(Request(scope))

        middleware = ProxyHeadersMiddleware(downstream, trusted_hosts=trusted)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/auth/login",
            "raw_path": b"/auth/login",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"x-forwarded-proto", b"https")],
            "client": (peer, 43120),
            "server": ("hermes", 9119),
        }

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            return None

        await middleware(scope, receive, send)
        return observed["https"]

    assert asyncio.run(detected_scheme("172.18.0.9")) is True
    assert asyncio.run(detected_scheme("::1")) is True
    assert asyncio.run(detected_scheme("198.51.100.9")) is False


def test_public_url_aware_gate_requires_auth_for_loopback_proxy(monkeypatch):
    """The shared gate decision includes an external browser-facing URL."""
    from hermes_cli.web_server import should_require_dashboard_auth

    monkeypatch.setenv(
        "HERMES_DASHBOARD_PUBLIC_URL",
        "https://dashboard.example.test:9443",
    )
    assert should_require_dashboard_auth("127.0.0.1") is True


def test_public_url_aware_gate_preserves_local_only_mode(monkeypatch):
    """A loopback browser-facing URL does not change local token mode."""
    from hermes_cli.web_server import should_require_dashboard_auth

    monkeypatch.setenv(
        "HERMES_DASHBOARD_PUBLIC_URL",
        "http://localhost:9119",
    )
    assert should_require_dashboard_auth("127.0.0.1") is False


def test_allowed_hosts_do_not_enable_auth_for_loopback_proxy(monkeypatch):
    """A trusted Tailscale Host alias is not a public exposure declaration."""
    from hermes_cli.dashboard_auth import clear_providers

    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setattr(
        web_server,
        "load_config",
        lambda: {
            "dashboard": {
                "public_url": "",
                "allowed_hosts": ["macstudio.tailnet.example"],
            }
        },
    )
    clear_providers()
    captured = _stub_uvicorn_run(monkeypatch)
    _restore_app_state_after_test(
        monkeypatch,
        "auth_required",
        "bound_host",
        "bound_port",
        "trusted_public_hosts",
    )

    web_server.start_server(
        host="127.0.0.1", port=9119,
        open_browser=False, allow_public=False,
    )

    assert web_server.app.state.auth_required is False
    assert web_server.app.state.trusted_public_hosts == frozenset(
        {"macstudio.tailnet.example"}
    )
    assert captured["kwargs"].get("host") == "127.0.0.1"


def test_malformed_public_url_is_not_trusted_as_a_host(monkeypatch):
    """A rejected public URL cannot bypass Host/Origin validation."""
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setattr(
        web_server,
        "load_config",
        lambda: {
            "dashboard": {
                "public_url": "dashboard.example.test",
                "allowed_hosts": [],
            }
        },
    )

    assert web_server._dashboard_public_hosts() == frozenset()
    assert web_server._configured_dashboard_allowed_hosts() == frozenset()


def test_effective_public_url_overrides_stale_config_host(monkeypatch):
    """Only the validated effective URL joins the Host/Origin trust set."""
    monkeypatch.setenv(
        "HERMES_DASHBOARD_PUBLIC_URL",
        "http://localhost:9119",
    )
    monkeypatch.setattr(
        web_server,
        "load_config",
        lambda: {
            "dashboard": {
                "public_url": "https://stale.example.test",
                "allowed_hosts": [],
            }
        },
    )

    assert web_server._dashboard_public_hosts() == frozenset({"localhost"})
    assert web_server._configured_dashboard_allowed_hosts() == frozenset()


def test_start_server_loopback_public_url_enables_gate(monkeypatch):
    """A declared external URL turns a loopback reverse proxy into gated mode."""
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

    monkeypatch.setenv(
        "HERMES_DASHBOARD_PUBLIC_URL",
        "https://dashboard.example.test:9443",
    )
    clear_providers()
    register_provider(StubAuthProvider())
    captured = _stub_uvicorn_run(monkeypatch)
    _restore_app_state_after_test(
        monkeypatch,
        "auth_required",
        "bound_host",
        "bound_port",
        "trusted_public_hosts",
    )
    try:
        web_server.start_server(
            host="127.0.0.1", port=9119,
            open_browser=False, allow_public=False,
        )
        assert web_server.app.state.auth_required is True
        assert web_server.app.state.trusted_public_hosts == frozenset(
            {"dashboard.example.test"}
        )
        assert captured["kwargs"].get("host") == "127.0.0.1"
        assert captured["kwargs"].get("proxy_headers") is True
    finally:
        clear_providers()


def test_start_server_loopback_public_url_without_provider_fails_closed(monkeypatch):
    """Trusting an external Host must never expose the loopback token mode."""
    from hermes_cli.dashboard_auth import clear_providers

    monkeypatch.setenv(
        "HERMES_DASHBOARD_PUBLIC_URL",
        "https://dashboard.example.test:9443",
    )
    clear_providers()
    _stub_uvicorn_run(monkeypatch)
    _restore_app_state_after_test(
        monkeypatch,
        "auth_required",
        "bound_host",
        "bound_port",
        "trusted_public_hosts",
    )

    with pytest.raises(SystemExit, match=r"no auth providers"):
        web_server.start_server(
            host="127.0.0.1", port=9119,
            open_browser=False, allow_public=False,
        )
    assert web_server.app.state.auth_required is True


def test_loopback_public_url_fail_closed_message_is_actionable(monkeypatch):
    """The refusal must name public_url, print its value, and give both exits.

    Upgrade compatibility: an operator with a stale dashboard.public_url and
    no auth provider must not face a mystery-locked dashboard — the error
    text IS the mitigation.
    """
    from hermes_cli.dashboard_auth import clear_providers

    monkeypatch.setenv(
        "HERMES_DASHBOARD_PUBLIC_URL",
        "https://dashboard.example.test:9443",
    )
    clear_providers()
    _stub_uvicorn_run(monkeypatch)
    _restore_app_state_after_test(
        monkeypatch,
        "auth_required",
        "bound_host",
        "bound_port",
        "trusted_public_hosts",
    )

    with pytest.raises(SystemExit) as exc:
        web_server.start_server(
            host="127.0.0.1", port=9119,
            open_browser=False, allow_public=False,
        )
    msg = str(exc.value)
    # Names the trigger and its value.
    assert "dashboard.public_url" in msg
    assert "https://dashboard.example.test:9443" in msg
    # Exit 1: configure auth.
    assert "basic_auth" in msg
    assert "hermes dashboard register" in msg
    # Exit 2: remove public_url to restore local-only mode.
    assert "remove dashboard.public_url" in msg
    assert "LOCAL-ONLY" in msg


@pytest.mark.parametrize("host,public_url,expected", [
    # Loopback bind, no public URL → local token mode, no gate.
    ("127.0.0.1", None, False),
    ("localhost", None, False),
    ("::1", None, False),
    # Loopback bind + non-loopback public URL → gate engages.
    ("127.0.0.1", "https://dash.example.test", True),
    ("::1", "https://dash.example.test:8443", True),
    # Loopback bind + loopback public URL → still local-only.
    ("127.0.0.1", "http://localhost:9119", False),
    ("127.0.0.1", "http://127.0.0.1:9119", False),
    # Non-loopback bind → always gated, public URL irrelevant.
    ("0.0.0.0", None, True),
    ("192.168.1.5", None, True),
    ("0.0.0.0", "http://localhost:9119", True),
])
def test_should_require_dashboard_auth_truth_table(
    monkeypatch, host, public_url, expected
):
    from hermes_cli.web_server import should_require_dashboard_auth

    if public_url is None:
        monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
        monkeypatch.setattr(
            web_server, "_dashboard_public_hosts", lambda: frozenset()
        )
    else:
        monkeypatch.setenv("HERMES_DASHBOARD_PUBLIC_URL", public_url)
    assert should_require_dashboard_auth(host) is expected
