import json

import pytest

from tools import browser_tool
from tools.authenticated_browser_projection import _reset_for_tests as _reset_abp_for_tests
from tools.in_app_browser_refs import _reset_for_tests


@pytest.fixture(autouse=True)
def reset_ref_state(monkeypatch):
    _reset_for_tests()
    _reset_abp_for_tests()
    browser_tool._active_sessions.clear()
    browser_tool._last_active_session_key.clear()
    browser_tool._active_sessions["task-ref"] = {
        "session_name": "in-app-test",
        "owner_task_id": "task-ref",
        "profile": "test-profile",
        "connection_id": "test-connection",
        "capability_generation": 1,
        "tab_id": "browser:tab-ref",
        "binding_generation": 1,
        "document_generation": 1,
        "guest_generation": "guest-1",
        "task_generation": 1,
        "features": {"in_app": True},
    }
    browser_tool._last_active_session_key["task-ref"] = "task-ref"
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(browser_tool, "_blocked_private_page_action", lambda *_args: None)
    yield
    browser_tool._active_sessions.clear()
    browser_tool._last_active_session_key.clear()
    _reset_for_tests()
    _reset_abp_for_tests()


def _snapshot(*rows):
    refs = {
        internal: {
            "backend_node_id": backend,
            "frame_id": frame,
            "name": f"secret-backend-{backend}",
        }
        for internal, backend, frame in rows
    }
    rendered = "\n".join(f'- button "row" [ref={internal}]' for internal, _backend, _frame in rows)
    return {"success": True, "data": {"snapshot": rendered, "refs": refs}}


def test_snapshot_then_actions_use_stable_external_refs_and_never_backend_ids(monkeypatch):
    results = iter(
        [
            _snapshot(("e1", 101, "frame-main"), ("e2", 202, "frame-main")),
            # Upstream recycles e1 for the surviving second node and e2 for a
            # new node. The adapter must expose e2 and a never-before-used e3.
            _snapshot(("e1", 202, "frame-main"), ("e2", 303, "frame-main")),
        ]
    )
    actions = []

    def run(_task, command, args=None, **_kwargs):
        if command == "snapshot":
            return next(results)
        actions.append((command, args))
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser_tool, "_run_browser_command", run)

    first = json.loads(browser_tool.browser_snapshot(task_id="task-ref"))
    second = json.loads(browser_tool.browser_snapshot(task_id="task-ref"))

    assert first["snapshot"].count("ref=e1") == 1
    assert first["snapshot"].count("ref=e2") == 1
    assert "101" not in json.dumps(first)
    assert "202" not in json.dumps(first)
    assert second["snapshot"].splitlines() == [
        '- button "row" [ref=e2]',
        '- button "row" [ref=e3]',
    ]

    assert json.loads(browser_tool.browser_click("@e2", task_id="task-ref")) == {
        "success": True,
        "clicked": "@e2",
    }
    typed = json.loads(browser_tool.browser_type("@e3", "hello", task_id="task-ref"))
    assert typed["success"] is True
    assert typed["element"] == "@e3"
    assert actions == [("click", ["@e1"]), ("fill", ["@e2", "hello"])]


def test_disappeared_and_prior_generation_refs_fail_closed_without_dispatch(monkeypatch):
    results = iter(
        [
            _snapshot(("e1", 101, "frame-main")),
            _snapshot(("e1", 202, "frame-main")),
            _snapshot(("e1", 303, "frame-main")),
        ]
    )
    actions = []

    def run(_task, command, args=None, **_kwargs):
        if command == "snapshot":
            return next(results)
        actions.append((command, args))
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser_tool, "_run_browser_command", run)

    assert "ref=e1" in json.loads(browser_tool.browser_snapshot(task_id="task-ref"))["snapshot"]
    assert "ref=e2" in json.loads(browser_tool.browser_snapshot(task_id="task-ref"))["snapshot"]
    stale = json.loads(browser_tool.browser_click("@e1", task_id="task-ref"))
    assert stale == {"success": False, "error": "STALE_REF: e1 is stale or unknown"}
    assert actions == []

    # A handoff/replacement generation gets a new registry but shares the
    # process-lifetime task allocator, so it cannot recycle e1/e2.
    browser_tool._active_sessions["task-ref"]["guest_generation"] = "guest-2"
    third = json.loads(browser_tool.browser_snapshot(task_id="task-ref"))
    assert "ref=e3" in third["snapshot"]
    assert json.loads(browser_tool.browser_click("@e2", task_id="task-ref"))["error"].startswith("STALE_REF:")
    assert actions == []


def test_gateway_restart_uses_a_fresh_visible_ref_incarnation(monkeypatch):
    actions = []

    def run(_task, command, args=None, **_kwargs):
        if command == "snapshot":
            return _snapshot(("e1", 101, "frame-main"))
        actions.append((command, args))
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser_tool, "_run_browser_command", run)

    before = json.loads(browser_tool.browser_snapshot(task_id="task-ref"))
    assert "ref=e1" in before["snapshot"]

    # Model a fresh gateway process while Desktop re-declares the identical
    # task/tab/guest generation. Persisted chat history may still contain e1.
    _reset_for_tests(incarnation_floor=1_000_000)
    after = json.loads(browser_tool.browser_snapshot(task_id="task-ref"))
    assert "ref=e1000001" in after["snapshot"]
    assert json.loads(browser_tool.browser_click("@e1", task_id="task-ref")) == {
        "success": False,
        "error": "STALE_REF: e1 is stale or unknown",
    }
    assert actions == []


def test_navigation_tombstones_refs_before_open_and_new_snapshot_never_reuses(monkeypatch):
    responses = iter(
        [
            _snapshot(("e1", 101, "frame-main")),
            {"success": True, "data": {"url": "https://next.example/", "title": "Next"}},
            _snapshot(("e1", 101, "frame-main")),
        ]
    )
    calls = []

    def run(_task, command, args=None, **_kwargs):
        calls.append((command, args))
        return next(responses)

    monkeypatch.setattr(browser_tool, "_run_browser_command", run)
    monkeypatch.setattr(browser_tool, "_get_session_info", lambda _task: browser_tool._active_sessions["task-ref"])
    monkeypatch.setattr(browser_tool, "_maybe_start_recording", lambda _task: None)
    monkeypatch.setattr(browser_tool, "_get_open_command_timeout", lambda **_kwargs: 10)
    monkeypatch.setattr(browser_tool, "check_website_access", lambda _url: None)
    monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda _url: False)

    assert "ref=e1" in json.loads(browser_tool.browser_snapshot(task_id="task-ref"))["snapshot"]
    navigated = json.loads(browser_tool.browser_navigate("https://next.example/", task_id="task-ref"))
    assert navigated["success"] is True
    assert "ref=e2" in navigated["snapshot"]
    assert json.loads(browser_tool.browser_click("@e1", task_id="task-ref"))["error"].startswith("STALE_REF:")
    assert calls[:3] == [
        ("snapshot", ["-c"]),
        ("open", ["https://next.example/"]),
        ("snapshot", ["-c"]),
    ]


def test_in_app_annotated_pixels_fail_instead_of_exposing_recycled_labels():
    raw = browser_tool.browser_vision("where", annotate=True, task_id="task-ref")
    assert isinstance(raw, str)
    result = json.loads(raw)
    assert result["success"] is False
    assert result["error"] == "CAPTURE_CONSENT_REQUIRED"


def test_weak_consumer_refs_are_atomically_enriched_and_stay_stable(monkeypatch):
    weak = {
        "success": True,
        "data": {
            "snapshot": '- textbox "Name" [ref=e1]',
            "refs": {"e1": {"role": "textbox", "name": "Name"}},
        },
    }
    captures = [
        [],
        [{
            "frame_id": "frame-main",
            "nodes": [{
                "backendDOMNodeId": 101,
                "role": {"value": "textbox"},
                "name": {"value": "Name"},
            }],
        }],
        [],
        [{
            "frame_id": "frame-main",
            "nodes": [{
                "backendDOMNodeId": 101,
                "role": {"value": "textbox"},
                "name": {"value": "Name"},
            }],
        }],
    ]
    browser_tool._active_sessions["task-ref"]["_snapshot_identity_provider"] = lambda: captures.pop(0)
    monkeypatch.setattr(browser_tool, "_run_browser_command", lambda *_args, **_kwargs: weak)

    first = json.loads(browser_tool.browser_snapshot(task_id="task-ref"))
    second = json.loads(browser_tool.browser_snapshot(task_id="task-ref"))
    assert "ref=e1" in first["snapshot"]
    assert second["snapshot"] == first["snapshot"]


def test_top_commit_invalidation_is_exact_generation_fenced(monkeypatch):
    actions = []

    def run(_task, command, args=None, **_kwargs):
        if command == "snapshot":
            return _snapshot(("e1", 101, "frame-main"))
        actions.append((command, args))
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser_tool, "_run_browser_command", run)
    browser_tool.browser_snapshot(task_id="task-ref")
    assert browser_tool.invalidate_in_app_browser_session_refs(
        task_id="task-ref", guest_generation="guest-stale", task_generation=1
    ) is False
    assert json.loads(browser_tool.browser_click("e1", task_id="task-ref"))["success"] is True

    assert browser_tool.invalidate_in_app_browser_session_refs(
        task_id="task-ref", guest_generation="guest-1", task_generation=1
    ) is True
    stale = json.loads(browser_tool.browser_click("e1", task_id="task-ref"))
    assert stale["error"] == "STALE_REF: e1 is stale or unknown"
    assert actions == [("click", ["@e1"])]
