"""Tests for compact gateway tool-progress HUD helpers and cleanup."""

from collections import OrderedDict
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter


class _DummyAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="dummy"), Platform.TELEGRAM)

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        raise NotImplementedError

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id: str):
        return None


def test_render_compact_tool_progress_counts_and_order():
    rendered = gateway_run._render_compact_tool_progress(
        OrderedDict([
            ("python", 3),
            ("web", 2),
            ("files", 1),
        ])
    )

    assert rendered == "🐍×3 Using Python\n🌐×2 Searching the web\n📁×1 Using files"


def test_render_compact_tool_progress_uses_cloud_for_thinking_and_brain_for_memory():
    rendered = gateway_run._render_compact_tool_progress(
        OrderedDict([
            ("thinking", 1),
            ("memory", 2),
        ])
    )

    assert rendered == "☁️×1 Thinking\n🧠×2 Using memory"


def test_render_compact_tool_progress_falls_back_to_generic_tool_label():
    rendered = gateway_run._render_compact_tool_progress(OrderedDict([("mystery_tool", 4)]))

    assert rendered == "⚙️×4 Using tools"


def test_group_compact_progress_tool_matches_memory_and_agents_buckets():
    from agent.display import group_compact_progress_tool

    assert group_compact_progress_tool("mem0_search") == "memory_read"
    assert group_compact_progress_tool("session_search") == "memory_read"
    assert group_compact_progress_tool("delegate_task") == "agents"
    assert group_compact_progress_tool("codex_subtask") == "codex"
    assert group_compact_progress_tool("codex_subtask", {"action": "create"}) == "codex"
    assert group_compact_progress_tool("codex_subtask", {"action": "send"}) == "codex"
    assert group_compact_progress_tool("codex_subtask", {"action": "status"}) == "processes"
    assert group_compact_progress_tool("codex_subtask_await") == "processes"


def test_group_compact_progress_tool_detects_external_agent_cli_lanes():
    from agent.display import group_compact_progress_tool, render_compact_progress_summary

    assert group_compact_progress_tool("terminal", {"command": "agy -p 'audit this'"}) == "antigravity"
    assert group_compact_progress_tool("terminal", {"command": "cc --print 'audit this'"}) == "claude_code"
    assert group_compact_progress_tool("terminal", {"command": "claude --print 'audit this'"}) == "claude_code"
    assert group_compact_progress_tool("terminal", {"command": "script -q /tmp/out.typescript claude --print 'audit this'"}) == "claude_code"
    assert group_compact_progress_tool("terminal", {"command": "codex --help"}) == "codex"
    assert group_compact_progress_tool("terminal", {"command": "/opt/homebrew/bin/codex exec 'review'"}) == "codex"
    assert group_compact_progress_tool("terminal", {"command": "timeout 60 /Users/Kosta/.local/bin/claude-code --print 'review'"}) == "claude_code"
    assert group_compact_progress_tool("terminal", {"command": "cd /repo && git status"}) == "git"
    assert group_compact_progress_tool("terminal", {"command": "bash -lc 'pytest tests/gateway/test_tool_progress_compact.py'"}) == "tests"
    assert group_compact_progress_tool("terminal", {"command": "cd app && /opt/homebrew/bin/gh pr view"}) == "github"

    rendered = render_compact_progress_summary(OrderedDict([
        ("antigravity", 1),
        ("codex", 2),
        ("claude_code", 3),
    ]))
    assert rendered == "✨×1 Running Antigravity\n🌀×2 Running Codex\n☀️×3 Running Claude Code"


def test_group_compact_progress_tool_maps_all_web_lookup_tools_to_web_bucket():
    from agent.display import group_compact_progress_tool, render_compact_progress_summary

    for tool_name in [
        "web",
        "web_search",
        "web_extract",
        "fast_web_search",
        "fetch_page_clean",
        "scrape_page_answer",
        "mcp_exa_web_search_exa",
        "mcp_exa_web_fetch_exa",
        "mcp_firecrawl_firecrawl_search",
        "mcp_firecrawl_firecrawl_scrape",
        "mcp_firecrawl_firecrawl_extract",
        "mcp_firecrawl_firecrawl_map",
        "mcp_firecrawl_firecrawl_agent_status",
    ]:
        assert group_compact_progress_tool(tool_name) == "web"

    rendered = render_compact_progress_summary(OrderedDict([("web", 12)]))
    assert rendered == "🌐×12 Searching the web"


def test_group_compact_progress_tool_maps_x_tools_to_bird_bucket():
    from agent.display import group_compact_progress_tool, render_compact_progress_summary

    assert group_compact_progress_tool("x_twitter") == "x"
    assert group_compact_progress_tool("x_search") == "x"
    assert group_compact_progress_tool("grok_research") == "x"
    assert group_compact_progress_tool("xurl") == "x"
    rendered = render_compact_progress_summary(OrderedDict([("x", 2)]))
    assert rendered == "🐦×2 Using X/Grok"


def test_group_compact_progress_tool_maps_common_gateway_tools_to_specific_buckets():
    from agent.display import group_compact_progress_tool, render_compact_progress_summary

    expected = {
        "computer_use": "macos",
        "swiftui_preview": "apple_dev",
        "send_message": "messaging",
        "telegram_actions": "messaging",
        "discord_admin": "messaging",
        "yb_send_dm": "messaging",
        "feishu_drive_add_comment": "messaging",
        "ha_get_state": "homeassistant",
        "kanban_create": "kanban",
        "mixture_of_agents": "agents",
        "image_process": "images",
        "video_generate": "video",
        "video_analyze": "video_analysis",
        "pdf_parse": "documents",
        "feishu_doc_read": "documents",
        "plik": "file_sharing",
        "tools": "tools",
        "github_repo_brief": "github",
        "curlmd_fetch": "web",
    }
    for tool_name, bucket in expected.items():
        assert group_compact_progress_tool(tool_name) == bucket

    assert group_compact_progress_tool("fs", {"action": "read"}) == "files_read"
    assert group_compact_progress_tool("fs", {"action": "search"}) == "files_search"
    assert group_compact_progress_tool("fs", {"action": "patch"}) == "files_write"
    assert group_compact_progress_tool("guest_fs", {"action": "read"}) == "files_read"
    assert group_compact_progress_tool("guest_fs", {"action": "search"}) == "files_search"
    assert group_compact_progress_tool("guest_fs", {"action": "write"}) == "files_write"
    assert group_compact_progress_tool("skill", {"action": "list"}) == "skills_read"
    assert group_compact_progress_tool("skill", {"action": "view"}) == "skills_read"
    assert group_compact_progress_tool("skill", {"action": "manage"}) == "skills_write"
    assert group_compact_progress_tool("terminal", {"command": "xcodebuild test"}) == "apple_dev"
    assert group_compact_progress_tool("terminal", {"command": "xcrun simctl list"}) == "apple_dev"

    rendered = render_compact_progress_summary(
        OrderedDict([
            ("macos", 1),
            ("apple_dev", 2),
            ("messaging", 1),
            ("video", 1),
        ])
    )
    assert "🖥️×1 Using Mac" in rendered
    assert "🍎×2 Using Apple dev tools" in rendered
    assert "💬×1 Messaging" in rendered
    assert "🎬×1 Making video" in rendered

    labels = render_compact_progress_summary(
        OrderedDict([
            ("homeassistant", 1),
            ("kanban", 1),
            ("video_analysis", 1),
            ("documents", 1),
            ("file_sharing", 1),
            ("tools", 1),
        ])
    )
    assert "🏠×1 Using Home Assistant" in labels
    assert "🗂️×1 Using kanban" in labels
    assert "🎞️×1 Analyzing video" in labels
    assert "📄×1 Reading documents" in labels
    assert "📤×1 Sharing files" in labels
    assert "🧰×1 Inspecting tools" in labels


def test_gateway_compact_progress_update_groups_repeated_tools():
    counts = OrderedDict()

    first = gateway_run._update_compact_tool_progress(counts, "web_search")
    second = gateway_run._update_compact_tool_progress(counts, "fast_web_search")
    third = gateway_run._update_compact_tool_progress(counts, "terminal", {"command": "git status"})

    assert first == "🌐×1 Searching the web"
    assert second == "🌐×2 Searching the web"
    assert third == "🌐×2 Searching the web\n🌿×1 Using git"


def test_gateway_compact_progress_todo_renders_task_card_and_persists_with_later_tools():
    counts = OrderedDict()
    todo_args = {"todos": [
        {"content": "Check compact HUD emoji mapping", "status": "in_progress"},
        {"content": "Commit verified restore", "status": "pending"},
    ]}

    first = gateway_run._update_compact_tool_progress(counts, "todo", todo_args)
    second = gateway_run._update_compact_tool_progress(
        counts,
        "search_files",
        {"pattern": "todo"},
        todo_args=todo_args,
    )

    assert first == "📋 Updating tasks\n◐ Check compact HUD emoji mapping\n☐ Commit verified restore"
    assert "🔎×1 Searching files" in second
    assert "📋 Updating tasks" in second
    assert "◐ Check compact HUD emoji mapping" in second
    assert "☐ Commit verified restore" in second


def test_group_compact_progress_tool_restores_skill_and_todo_buckets():
    from agent.display import group_compact_progress_tool, render_compact_progress_summary

    assert group_compact_progress_tool("skill_view") == "skills_read"
    assert group_compact_progress_tool("skills_list") == "skills_read"
    assert group_compact_progress_tool("skill_manage") == "skills_write"
    assert group_compact_progress_tool("todo") == "tasks"

    rendered = render_compact_progress_summary(
        OrderedDict([
            ("skills_read", 1),
            ("skills_write", 1),
            ("tasks", 1),
            ("memory_read", 1),
            ("files_write", 1),
        ])
    )
    assert "📜×1 Reading skills" in rendered
    assert "📝×1 Writing skills" in rendered
    assert "📋×1 Updating tasks" in rendered
    assert "🧠×1 Reading memory" in rendered
    assert "✏️×1 Editing files" in rendered


def test_render_todo_checklist_progress_shows_task_statuses():
    from agent.display import render_todo_checklist_progress

    rendered = render_todo_checklist_progress({"todos": [
        {"content": "Check compact HUD emoji mapping", "status": "in_progress"},
        {"content": "Commit verified restore", "status": "pending"},
    ]})

    assert rendered.startswith("📋 Updating tasks")
    assert "◐ Check compact HUD emoji mapping" in rendered
    assert "☐ Commit verified restore" in rendered


def test_gateway_compact_progress_counts_subagent_tool_events_as_real_tool_lanes():
    counts = OrderedDict()

    first = gateway_run._update_compact_tool_progress(
        counts,
        "terminal",
        {"command": "/opt/homebrew/bin/codex exec 'review'"},
    )
    second = gateway_run._update_compact_tool_progress(
        counts,
        "terminal",
        {"command": "timeout 60 /Users/Kosta/.local/bin/claude-code --print 'review'"},
    )

    assert first == "🌀×1 Running Codex"
    assert second == "🌀×1 Running Codex\n☀️×1 Running Claude Code"


@pytest.mark.asyncio
async def test_cleanup_pending_ephemeral_messages_deletes_and_clears_queue():
    adapter = _DummyAdapter()
    adapter.delete_message = AsyncMock(return_value=True)
    adapter._pending_ephemeral_deletes["session-1"] = [("chat-1", "msg-1")]

    await adapter._cleanup_pending_ephemeral_messages("session-1")

    adapter.delete_message.assert_awaited_once_with(chat_id="chat-1", message_id="msg-1")
    assert "session-1" not in adapter._pending_ephemeral_deletes
