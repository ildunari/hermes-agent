"""Regression tests for gateway session cwd binding during _run_agent()."""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from tools.terminal_tool import clear_task_env_overrides, register_task_env_overrides

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionEntry, SessionSource


class SilentAdapter(BasePlatformAdapter):
    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="m1")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class CwdCaptureAgent:
    captured: dict | None = None

    def __init__(self, **kwargs):
        self.tools = []
        self.session_id = kwargs.get("session_id")

    def run_conversation(self, message, conversation_history=None, task_id=None):
        from tools.terminal_tool import get_task_env_override

        CwdCaptureAgent.captured = {
            "session_id": self.session_id,
            "task_id": task_id,
            "cwd": get_task_env_override(task_id, "cwd"),
        }
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


@pytest.mark.asyncio
async def test_run_agent_rebinds_session_cwd_override(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CwdCaptureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    adapter = SilentAdapter()
    runner = _make_runner(adapter)

    project_dir = tmp_path / "craft-ios-companion"
    project_dir.mkdir()

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="12345",
    )
    session_entry = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-cwd",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        cwd_override=str(project_dir),
    )
    runner.session_store = MagicMock()
    runner.session_store.get_session.return_value = None
    runner.session_store.get_or_create_session.return_value = session_entry

    clear_task_env_overrides("sess-cwd")
    CwdCaptureAgent.captured = None
    try:
        result = await runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=source,
            session_id="sess-cwd",
            session_key=session_entry.session_key,
        )
    finally:
        clear_task_env_overrides("sess-cwd")

    assert result["final_response"] == "done"
    assert CwdCaptureAgent.captured == {
        "session_id": "sess-cwd",
        "task_id": "sess-cwd",
        "cwd": str(project_dir),
    }


@pytest.mark.asyncio
async def test_run_agent_prefers_exact_session_key_binding(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CwdCaptureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    adapter = SilentAdapter()
    runner = _make_runner(adapter)

    exact_dir = tmp_path / "exact-project"
    fallback_dir = tmp_path / "fallback-project"
    exact_dir.mkdir()
    fallback_dir.mkdir()

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="12345",
    )
    exact_entry = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-cwd",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        cwd_override=str(exact_dir),
    )
    fallback_entry = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-cwd",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        cwd_override=str(fallback_dir),
    )
    runner.session_store = MagicMock()
    runner.session_store.get_session.return_value = exact_entry
    runner.session_store.get_or_create_session.return_value = fallback_entry

    clear_task_env_overrides("sess-cwd")
    CwdCaptureAgent.captured = None
    try:
        result = await runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=source,
            session_id="sess-cwd",
            session_key=exact_entry.session_key,
        )
    finally:
        clear_task_env_overrides("sess-cwd")

    assert result["final_response"] == "done"
    assert CwdCaptureAgent.captured == {
        "session_id": "sess-cwd",
        "task_id": "sess-cwd",
        "cwd": str(exact_dir),
    }
    runner.session_store.get_session.assert_called_once_with(exact_entry.session_key)
    runner.session_store.get_or_create_session.assert_not_called()


@pytest.mark.asyncio
async def test_run_agent_replaces_stale_task_cwd_with_session_binding(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CwdCaptureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    adapter = SilentAdapter()
    runner = _make_runner(adapter)

    project_dir = tmp_path / "craft-ios-companion"
    project_dir.mkdir()

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="12345",
    )
    session_entry = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-cwd",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        cwd_override=str(project_dir),
    )
    runner.session_store = MagicMock()
    runner.session_store.get_session.return_value = session_entry
    runner.session_store.get_or_create_session.return_value = session_entry

    clear_task_env_overrides("sess-cwd")
    register_task_env_overrides("sess-cwd", {"cwd": str(tmp_path / "wrong-project")})
    CwdCaptureAgent.captured = None
    try:
        result = await runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=source,
            session_id="sess-cwd",
            session_key=session_entry.session_key,
        )
    finally:
        clear_task_env_overrides("sess-cwd")

    assert result["final_response"] == "done"
    assert CwdCaptureAgent.captured == {
        "session_id": "sess-cwd",
        "task_id": "sess-cwd",
        "cwd": str(project_dir),
    }
