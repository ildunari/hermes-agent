import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.dashboard_auth import token_auth
from hermes_cli.web_routers import maintenance as routes
from plugins.dashboard_auth.drain import DrainSecretProvider
from tui_gateway import owner_maintenance


@pytest.fixture
def api(tmp_path, monkeypatch):
    owner = owner_maintenance.OwnerMaintenance(tmp_path)
    monkeypatch.setattr(owner_maintenance, "_owner", owner)
    monkeypatch.setattr(routes, "_child", lambda *a, **k: None)
    monkeypatch.setattr(routes, "local_status", owner.status)
    secret = secrets.token_urlsafe(48)
    monkeypatch.setattr(token_auth, "list_token_providers", lambda: [DrainSecretProvider(secret=secret)])
    for action in ("begin", "status", "release"):
        token_auth.register_token_route(f"/api/maintenance/{action}")
    app = FastAPI()
    app.include_router(routes.router)
    app.middleware("http")(token_auth.token_auth_middleware)
    return TestClient(app), owner, {"Authorization": f"Bearer {secret}"}


def test_authenticated_generation_and_two_phase_release(api):
    client, owner, headers = api
    path = "/api/maintenance/"
    assert client.post(path + "status", json={}).status_code == 401
    assert client.post(path + "begin", json={}, headers=headers).status_code == 409
    body = {"owner_generation": owner.generation, "request_token": "operation"}
    reply = client.post(path + "begin", json=body, headers=headers)
    assert reply.status_code == 200
    assert reply.json()["owners"][0]["admissions_closed"]
    assert client.post(path + "release", json={**body, "request_token": "wrong"}, headers=headers).status_code == 409
    reply = client.post(path + "release", json=body, headers=headers)
    assert not reply.json()["owners"][0]["admissions_closed"]
    assert reply.json()["owners"][0]["bootstrap_held"]
    final = {**body, "finalize_bootstrap": True, "expected_owner_generations": [owner.generation]}
    assert client.post(path + "release", json={**final, "expected_owner_generations": ["stale"]}, headers=headers).status_code == 409
    for _ in range(2):
        reply = client.post(path + "release", json=final, headers=headers)
        assert reply.status_code == 200
        assert not reply.json()["owners"][0]["bootstrap_held"]
        assert reply.json()["owners"][0]["released_request_token"] == "operation"


def test_unavailable_child_fails_closed(api, monkeypatch):
    client, owner, headers = api
    def unavailable(*a, **k):
        raise RuntimeError("owner unreachable")
    monkeypatch.setattr(routes, "_child", unavailable)
    response = client.post("/api/maintenance/status", json={}, headers=headers)
    assert response.status_code == 503
