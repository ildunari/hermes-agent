"""Regression coverage for reload-stable browser lifecycle ownership."""

from __future__ import annotations

import importlib
import time


def test_reload_keeps_one_cleanup_thread_and_reaps_current_shared_state(monkeypatch):
    import tools.browser_session_state as state
    import tools.browser_tool as browser_tool

    state.ACTIVE_SESSIONS.clear()
    state.RECORDING_SESSIONS.clear()
    state.LAST_ACTIVE_SESSION_KEY.clear()
    state.IN_APP_SESSION_EXPECTATIONS.clear()
    state.SESSION_LAST_ACTIVITY.clear()
    state.CLEANUP_RUNTIME.done = False

    monkeypatch.setattr(browser_tool, "_reap_orphaned_browser_sessions", lambda: None)
    browser_tool._start_browser_cleanup_thread()
    thread = state.CLEANUP_RUNTIME.thread
    assert thread is not None and thread.is_alive()

    browser_tool = importlib.reload(browser_tool)
    browser_tool._start_browser_cleanup_thread()
    assert state.CLEANUP_RUNTIME.thread is thread
    assert browser_tool._active_sessions is state.ACTIVE_SESSIONS
    assert browser_tool._cleanup_lock is state.CLEANUP_LOCK
    assert browser_tool._in_app_session_condition is state.IN_APP_SESSION_CONDITION
    assert state.CLEANUP_RUNTIME.emergency_callback is browser_tool._emergency_cleanup_all_sessions
    assert state.CLEANUP_RUNTIME.stop_callback is browser_tool._stop_browser_cleanup_thread

    state.ACTIVE_SESSIONS["stale"] = {"session_name": "stale"}
    state.LAST_ACTIVE_SESSION_KEY["stale"] = "stale"
    state.IN_APP_SESSION_EXPECTATIONS["stale"] = time.monotonic() + 30
    state.SESSION_LAST_ACTIVITY["stale"] = (
        time.time() - browser_tool.BROWSER_SESSION_INACTIVITY_TIMEOUT - 1
    )
    reaped: list[str] = []

    def cleanup(task_id: str) -> None:
        reaped.append(task_id)
        state.ACTIVE_SESSIONS.pop(task_id, None)
        state.LAST_ACTIVE_SESSION_KEY.pop(task_id, None)
        state.IN_APP_SESSION_EXPECTATIONS.pop(task_id, None)

    monkeypatch.setattr(browser_tool, "cleanup_browser", cleanup)
    browser_tool._cleanup_inactive_browser_sessions()
    assert reaped == ["stale"]
    assert "stale" not in state.SESSION_LAST_ACTIVITY

    browser_tool._stop_browser_cleanup_thread()
    assert not thread.is_alive()
