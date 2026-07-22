from __future__ import annotations

import threading
import time

import pytest

import tools.browser_tool as browser_tool


@pytest.fixture(autouse=True)
def isolate_browser_sessions(monkeypatch):
    monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser_tool, "_update_session_activity", lambda _task_id: None)
    with browser_tool._in_app_session_condition:
        original_sessions = dict(browser_tool._active_sessions)
        original_expectations = dict(browser_tool._in_app_session_expectations)
        browser_tool._active_sessions.clear()
        browser_tool._in_app_session_expectations.clear()

    yield

    with browser_tool._in_app_session_condition:
        browser_tool._active_sessions.clear()
        browser_tool._active_sessions.update(original_sessions)
        browser_tool._in_app_session_expectations.clear()
        browser_tool._in_app_session_expectations.update(original_expectations)
        browser_tool._in_app_session_condition.notify_all()


def register(task_id: str) -> None:
    browser_tool.register_in_app_browser_session(
        task_id=task_id,
        cdp_url="ws://127.0.0.1:9911/automation-token",
        raw_cdp_url="ws://127.0.0.1:9911/raw-token",
        profile="coding",
        connection_id="connection-1",
        capability_generation=1,
        tab_id="tab-1",
        binding_generation=1,
        guest_generation="guest-1",
        task_generation=1,
    )


def test_expected_session_waits_for_exact_in_app_registration():
    task_id = "desktop-session"
    browser_tool.expect_in_app_browser_session(task_id, timeout=1)
    result: list[dict] = []

    waiter = threading.Thread(target=lambda: result.append(browser_tool._get_session_info(task_id)))
    waiter.start()
    time.sleep(0.03)

    assert waiter.is_alive()
    register(task_id)
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert result[0]["features"]["in_app"] is True
    assert result[0]["owner_task_id"] == task_id


def test_expected_session_times_out_without_falling_back(monkeypatch):
    local_calls: list[str] = []
    monkeypatch.setattr(browser_tool, "_create_local_session", lambda task_id: local_calls.append(task_id) or {})
    browser_tool.expect_in_app_browser_session("missing-desktop", timeout=0.02)

    with pytest.raises(RuntimeError, match="in-app browser relay did not bind"):
        browser_tool._get_session_info("missing-desktop")

    assert local_calls == []
    assert "missing-desktop" not in browser_tool._active_sessions
