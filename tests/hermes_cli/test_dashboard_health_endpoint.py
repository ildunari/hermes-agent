"""Focused contract tests for the dashboard's constant-time health probe."""

from __future__ import annotations

import asyncio
import sys
import types

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS



def test_active_turn_snapshot_counts_only_running_sessions(monkeypatch):
    from tui_gateway import server

    now = 1_000.0
    monkeypatch.setattr(server.time, "time", lambda: now)
    monkeypatch.setattr(
        server,
        "_sessions",
        {
            "active": {
                "running": True,
                "inflight_turn": {"started_at": 958.5},
                "last_active": 990.0,
            },
            "idle": {"running": False, "last_active": 900.0},
        },
    )

    assert server.active_turn_snapshot() == {
        "active_runs": 1,
        "oldest_run_age_seconds": 41.5,
    }


def test_health_stays_live_while_tui_gateway_module_is_loading(monkeypatch):
    monkeypatch.setitem(sys.modules, "tui_gateway.server", types.SimpleNamespace())

    payload = asyncio.run(web_server.get_health())

    assert payload == {
        "status": "ok",
        "marker": web_server.DASHBOARD_HEALTH_MARKER,
        "active_runs": 0,
    }


def test_health_is_public_static_json_and_not_spa_fallback(monkeypatch):
    previous_required = getattr(web_server.app.state, "auth_required", None)
    previous_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "dashboard.example.test"

    # These are representative expensive dependencies used by /api/status.
    # A health request must not reach any of them.
    monkeypatch.setattr(
        web_server,
        "check_config_version",
        lambda: (_ for _ in ()).throw(AssertionError("config read from /health")),
    )
    monkeypatch.setattr(
        web_server,
        "get_running_pid_cached",
        lambda: (_ for _ in ()).throw(AssertionError("PID probe from /health")),
    )
    monkeypatch.setattr(
        "tui_gateway.server.active_turn_snapshot",
        lambda: {"active_runs": 2, "oldest_run_age_seconds": 41.5},
    )

    try:
        response = TestClient(
            web_server.app, base_url="https://dashboard.example.test"
        ).get("/health")
    finally:
        web_server.app.state.auth_required = previous_required
        web_server.app.state.bound_host = previous_host

    assert "/health" in PUBLIC_API_PATHS
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "status": "ok",
        "marker": web_server.DASHBOARD_HEALTH_MARKER,
        "active_runs": 2,
        "oldest_run_age_seconds": 41.5,
    }
    assert web_server.DASHBOARD_HEALTH_MARKER == "hermes-dashboard-ok"
    assert "<html" not in response.text.lower()
    assert web_server._SESSION_TOKEN not in response.text


def test_health_reports_idle_dashboard_turns(monkeypatch):
    monkeypatch.setattr(
        "tui_gateway.server.active_turn_snapshot",
        lambda: {"active_runs": 0},
    )

    response = TestClient(web_server.app).get("/health")

    assert response.status_code == 200
    assert response.json()["active_runs"] == 0
    assert "oldest_run_age_seconds" not in response.json()



def test_health_is_distinct_from_unchanged_api_status():
    """Each liveness/status route must retain its own handler identity."""
    routes = {
        path: getattr(route, "endpoint", None)
        for route in web_server.app.routes
        if (path := getattr(route, "path", None))
        in {"/health", "/api/health", "/api/status"}
    }

    assert routes["/health"] is web_server.get_health
    assert routes["/api/health"] is web_server.get_api_health
    assert routes["/api/status"] is web_server.get_status
    assert len(set(routes.values())) == 3
