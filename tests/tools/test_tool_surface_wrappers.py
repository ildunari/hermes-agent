"""Tests for compact fs/web/tools tool-surface wrappers."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from hermes_cli.tools_config import _get_platform_tools
from model_tools import get_tool_definitions, handle_function_call
from tools.web_tools import _handle_web
from toolsets import resolve_toolset


def _tool_names(tool_defs):
    return {tool["function"]["name"] for tool in tool_defs}


def test_dedicated_file_tools_visible_and_fs_wrapper_retired():
    enabled = sorted(_get_platform_tools({"platform_toolsets": {"telegram": ["no_mcp", "file"]}}, "telegram"))
    names = _tool_names(get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True))

    assert {"read_file", "write_file", "patch", "search_files"} <= names
    assert "fs" not in names


def test_dedicated_file_tools_dispatch_read_write_patch_search(tmp_path):
    target_dir = tmp_path
    if str(target_dir).startswith("/private/var/") or str(target_dir).startswith("/var/"):
        target_dir = Path.cwd() / ".pytest-fs-wrapper"
        target_dir.mkdir(exist_ok=True)
    target = target_dir / "sample.txt"

    write_result = json.loads(handle_function_call("write_file", {"path": str(target), "content": "alpha\nbeta\n"}))
    assert write_result["bytes_written"] == len("alpha\nbeta\n")

    read_result = json.loads(handle_function_call("read_file", {"path": str(target), "limit": 5}))
    assert "1|alpha" in read_result["content"]

    patch_result = json.loads(handle_function_call("patch", {"mode": "replace", "path": str(target), "old_string": "beta", "new_string": "gamma"}))
    assert patch_result["success"] is True

    search_result = json.loads(handle_function_call("search_files", {"pattern": "gamma", "path": str(target_dir)}))
    assert search_result["total_count"] >= 1
    if target_dir.name == ".pytest-fs-wrapper":
        shutil.rmtree(target_dir, ignore_errors=True)


def test_web_wrapper_visible_with_search_affordance_and_extract_legacy_hidden_by_default(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    enabled = sorted(_get_platform_tools({"platform_toolsets": {"telegram": ["no_mcp", "web"]}}, "telegram"))
    names = _tool_names(get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True))

    assert "web" in names
    assert "web_search" in names
    assert {"web_extract", "curlmd_fetch"}.isdisjoint(names)


def test_search_toolset_is_search_only():
    assert set(resolve_toolset("search")) == {"web_search"}


def test_web_wrapper_dispatches_search_and_extract_modes(monkeypatch):
    calls = []

    def fake_search(query, limit=5):
        calls.append(("search", query, limit))
        return json.dumps({"data": {"web": [{"title": "ok"}]}})

    async def fake_extract(urls, **kwargs):
        calls.append(("extract", urls, kwargs))
        return json.dumps({"results": [{"url": urls[0], "content": "ok"}]})

    monkeypatch.setattr("tools.web_tools.web_search_tool", fake_search)
    monkeypatch.setattr("tools.web_tools.web_extract_tool", fake_extract)
    monkeypatch.setattr("tools.web_tools.check_web_api_key", lambda: True)

    search = asyncio.run(_handle_web({"action": "search", "query": "hermes", "limit": 2}))
    fetch = asyncio.run(_handle_web({"action": "fetch", "urls": ["https://example.com"]}))
    answer = asyncio.run(_handle_web({"action": "answer", "urls": ["https://example.com"], "question": "why?"}))

    assert json.loads(search)["data"]["web"][0]["title"] == "ok"
    assert json.loads(fetch)["results"][0]["content"] == "ok"
    assert calls[0] == ("search", "hermes", 2)
    assert calls[1][2]["mode"] == "markdown"
    assert calls[2][2]["mode"] == "answer"
    assert calls[2][2]["question"] == "why?"


def test_web_wrapper_rejects_fetch_with_singular_url():
    result = json.loads(asyncio.run(_handle_web({"action": "fetch", "url": "https://example.com"})))
    assert "error" in result
    assert "requires 'urls' as a list" in result["error"]


def test_web_wrapper_search_reports_missing_backend(monkeypatch):
    monkeypatch.setattr("tools.web_tools.check_web_api_key", lambda: False)
    result = json.loads(asyncio.run(_handle_web({"action": "search", "query": "hermes"})))
    assert "error" in result
    assert "requires a configured web search backend" in result["error"]
    assert "web_search" not in result["error"]


def test_webhook_default_toolset_keeps_web_surface_search_only(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    enabled = ["hermes-webhook"]
    names = _tool_names(get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True))

    assert {"web_search", "clarify"}.issubset(names)
    # vision_analyze is intentionally gated on vision-provider availability;
    # webhook must not depend on it for the web safety invariant.
    assert "web" not in names
    assert "web_extract" not in names
    assert "curlmd_fetch" not in names


def test_tools_meta_lists_and_describes_without_secret_values():
    listed = json.loads(handle_function_call("tools", {"action": "list", "category": "browser", "platform": "telegram"}))
    assert "browser" in listed["categories"]
    assert any(item["name"] == "browser_cdp" for item in listed["categories"]["browser"]["advanced_tools"])

    described = json.loads(handle_function_call("tools", {"action": "describe", "name": "read_file"}))
    assert described["name"] == "read_file"
    assert "path" in described["parameters"]

    config = json.loads(handle_function_call("tools", {"action": "config", "platform": "telegram"}))
    assert config["platform"] == "telegram"
    serialized = json.dumps(config).lower()
    assert "op_service_account_token" not in serialized
    assert "api_key=" not in serialized
