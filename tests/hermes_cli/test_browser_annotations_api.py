"""Authenticated active-profile boundary for browser annotation API routes."""

from types import SimpleNamespace

import pytest


def _request(tmp_path, profile="coding"):
    app = SimpleNamespace(
        state=SimpleNamespace(
            browser_active_profile=profile,
            browser_active_home=str(tmp_path),
        )
    )
    return SimpleNamespace(app=app, state=SimpleNamespace(), headers={})


def test_annotation_routes_are_registered():
    from hermes_cli.web_server import app

    methods_by_path = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/browser/annotations", "POST") in methods_by_path
    assert ("/api/browser/annotations", "GET") in methods_by_path
    assert ("/api/browser/annotations/{annotation_id}", "GET") in methods_by_path
    assert ("/api/browser/annotations/{annotation_id}/status", "PATCH") in methods_by_path
    assert ("/api/browser/annotations/{annotation_id}/reattach", "POST") in methods_by_path
    assert ("/api/browser/annotations/{annotation_id}/export", "GET") in methods_by_path
    assert ("/api/browser/annotations/{annotation_id}/thread/messages", "POST") in methods_by_path
    assert ("/api/browser/annotations/{annotation_id}/thread", "GET") in methods_by_path
    assert (
        "/api/browser/annotations/{annotation_id}/thread/turns/{turn_id}",
        "GET",
    ) in methods_by_path
    assert (
        "/api/browser/annotations/{annotation_id}/thread/turns/{turn_id}/cancel",
        "POST",
    ) in methods_by_path
    assert (
        "/api/browser/annotations/{annotation_id}/thread/turns/{turn_id}/retry",
        "POST",
    ) in methods_by_path
    assert ("/api/browser/annotations/captures/{capture_id}", "PUT") in methods_by_path
    assert ("/api/browser/annotations/import", "POST") in methods_by_path
    assert ("/api/browser/annotations/{annotation_id}", "DELETE") in methods_by_path


def test_repository_uses_frozen_active_profile_home(monkeypatch, tmp_path):
    from hermes_cli import web_server

    request = _request(tmp_path)
    monkeypatch.setattr(web_server, "_http_auth_identity", lambda request: {"principal": "test"})

    repository = web_server._browser_annotation_repository(request, "coding")

    assert repository.profile_id == "coding"
    assert repository.db_path == (
        tmp_path / "browser" / "annotations" / "v1" / "annotations.v1.sqlite3"
    )
    assert repository.list_for_workspace("ws-1") == []


def test_repository_rejects_cross_profile_assertion(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from hermes_cli import web_server

    request = _request(tmp_path)
    monkeypatch.setattr(web_server, "_http_auth_identity", lambda request: {"principal": "test"})

    with pytest.raises(HTTPException) as exc_info:
        web_server._browser_annotation_repository(request, "other")

    assert exc_info.value.status_code == 403


def test_repository_requires_http_authentication(tmp_path):
    from fastapi import HTTPException
    from hermes_cli import web_server

    with pytest.raises(HTTPException) as exc_info:
        web_server._browser_annotation_repository(_request(tmp_path), "coding")

    assert exc_info.value.status_code == 401


def test_dedicated_rpc_singleton_is_authenticated_and_profile_frozen(
    monkeypatch, tmp_path
):
    from hermes_cli import web_server
    from hermes_state import SessionDB

    SessionDB(db_path=tmp_path / "state.db").close()
    request = _request(tmp_path)
    monkeypatch.setattr(
        web_server,
        "_http_auth_identity",
        lambda _request: {"principal": "dashboard:test"},
    )

    first = web_server._browser_annotation_rpc(request, "coding")
    second = web_server._browser_annotation_rpc(request, "coding")

    try:
        assert first is second
        assert first.profile_id == "coding"
        assert first.lineage.db_path == tmp_path / "state.db"
        assert first.registry.repository is first.lineage
    finally:
        first.registry.close()


def test_dedicated_rpc_rejects_cross_profile_before_startup(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from hermes_cli import web_server

    request = _request(tmp_path)
    monkeypatch.setattr(
        web_server,
        "_http_auth_identity",
        lambda _request: {"principal": "dashboard:test"},
    )
    monkeypatch.setattr(
        web_server,
        "_start_browser_annotation_rpc",
        lambda _app: pytest.fail("cross-profile assertion reached RPC startup"),
    )

    with pytest.raises(HTTPException) as exc_info:
        web_server._browser_annotation_rpc(request, "other")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "profile_mismatch"}


def test_thread_message_http_route_requires_auth_and_uses_dedicated_facade(monkeypatch):
    from fastapi.testclient import TestClient
    from hermes_cli import web_server

    calls = []

    class Registry:
        def close(self):
            pass

    class Facade:
        profile_id = "coding"
        registry = Registry()

        def submit_message(self, **kwargs):
            calls.append(kwargs)
            return {"outcome": "queued", "turnId": "turn-1"}

    facade = Facade()

    def freeze_profile(target_app):
        target_app.state.browser_active_profile = "coding"
        target_app.state.browser_active_home = "/tmp/active-coding"
        return "coding"

    monkeypatch.setattr(web_server, "_capture_browser_active_profile", freeze_profile)
    web_server.app.state.browser_active_profile = "coding"
    web_server.app.state.browser_active_home = "/tmp/active-coding"
    web_server.app.state.browser_annotation_rpc = facade
    monkeypatch.setattr(web_server, "_start_browser_annotation_rpc", lambda _app: facade)
    payload = {
        "profile": "coding",
        "thread_generation": 1,
        "body": "ask body",
        "intent": "ask_agent",
        "anchor_revision_id": "revision-1",
        "client_request_id": "request-1",
        "anchor_stale_at_submit": False,
    }

    try:
        with TestClient(web_server.app) as client:
            unauthenticated = client.post(
                "/api/browser/annotations/ann-1/thread/messages", json=payload
            )
            authenticated = client.post(
                "/api/browser/annotations/ann-1/thread/messages",
                json=payload,
                headers={
                    "X-Hermes-Session-Token": web_server._SESSION_TOKEN,
                },
            )
            malformed = client.post(
                "/api/browser/annotations/ann-1/thread/messages",
                json={**payload, "thread_generation": True},
                headers={
                    "X-Hermes-Session-Token": web_server._SESSION_TOKEN,
                },
            )
    finally:
        if getattr(web_server.app.state, "browser_annotation_rpc", None) is facade:
            del web_server.app.state.browser_annotation_rpc

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 202
    assert malformed.status_code == 422
    assert authenticated.json() == {"outcome": "queued", "turnId": "turn-1"}
    assert len(calls) == 1
    assert calls[0]["annotation_id"] == "ann-1"
    assert calls[0]["body"] == "ask body"
    assert calls[0]["actor_id"] == web_server._local_token_identity()["principal"]


def test_create_export_delete_http_wrappers_use_one_dedicated_bundle_facade(
    monkeypatch,
):
    from fastapi.testclient import TestClient
    from hermes_cli import web_server
    from hermes_cli.browser_annotations_models import AnnotationRecordV1, AnchorRevision

    calls = []

    class FakeModel:
        def model_copy(self, *, update):
            calls.append(("server_fields", update))
            return self

        def model_dump(self, **_kwargs):
            return {"annotationId": "ann-bundle", "thread": {"sessionLineageId": "root"}}

    monkeypatch.setattr(
        AnnotationRecordV1,
        "model_validate",
        classmethod(lambda _cls, _value: FakeModel()),
    )
    monkeypatch.setattr(
        AnchorRevision,
        "model_validate",
        classmethod(lambda _cls, _value: FakeModel()),
    )

    class Registry:
        def close(self):
            pass

    class Facade:
        profile_id = "coding"
        registry = Registry()

        def create_annotation(self, record, revision, *, source_message_id=None):
            calls.append(("create", record, revision, source_message_id))
            return record

        def export_bundle(self, annotation_id, *, include_screenshot_bytes=False):
            calls.append(("export", annotation_id, include_screenshot_bytes))
            return '{"kind":"frozen-bundle"}'

        def delete_bundle(self, annotation_id):
            calls.append(("delete", annotation_id))
            return {"outcome": "deleted", "threadDeleted": True}

    facade = Facade()

    def freeze_profile(target_app):
        target_app.state.browser_active_profile = "coding"
        target_app.state.browser_active_home = "/tmp/active-coding"
        return "coding"

    monkeypatch.setattr(web_server, "_capture_browser_active_profile", freeze_profile)
    web_server.app.state.browser_active_profile = "coding"
    web_server.app.state.browser_active_home = "/tmp/active-coding"
    web_server.app.state.browser_annotation_rpc = facade
    monkeypatch.setattr(web_server, "_start_browser_annotation_rpc", lambda _app: facade)
    headers = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}
    try:
        with TestClient(web_server.app) as client:
            created = client.post(
                "/api/browser/annotations",
                json={
                    "profile": "coding",
                    "record": {},
                    "first_revision": {},
                    "source_message_id": 42,
                },
                headers=headers,
            )
            exported = client.get(
                "/api/browser/annotations/ann-bundle/export",
                params={
                    "profile": "coding",
                    "include_screenshot_bytes": True,
                },
                headers=headers,
            )
            deleted = client.request(
                "DELETE",
                "/api/browser/annotations/ann-bundle",
                json={"profile": "coding"},
                headers=headers,
            )
    finally:
        if getattr(web_server.app.state, "browser_annotation_rpc", None) is facade:
            del web_server.app.state.browser_annotation_rpc

    assert created.status_code == 201
    assert exported.status_code == 200
    assert exported.json() == {"kind": "frozen-bundle"}
    assert deleted.status_code == 200
    assert deleted.json() == {"outcome": "deleted", "threadDeleted": True}
    assert any(item[0] == "create" and item[3] == 42 for item in calls)
    assert ("export", "ann-bundle", True) in calls
    assert ("delete", "ann-bundle") in calls
