"""Tests for CLI compact tool-progress rendering shared with the gateway HUD."""

import importlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_cli(tool_progress="compact", compact_progress_layout="multi_line", reasoning_style="status"):
    _clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {
            "compact": False,
            "tool_progress": tool_progress,
            "compact_progress_layout": compact_progress_layout,
            "reasoning_style": reasoning_style,
        },
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
        "prompt_toolkit.auto_suggest": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict("os.environ", clean_env, clear=False):
        import cli as mod

        mod = importlib.reload(mod)
        with patch.object(mod, "get_tool_definitions", return_value=[]), patch.dict(mod.__dict__, {"CLI_CONFIG": _clean_config}):
            return mod.HermesCLI()


def test_compact_progress_uses_gateway_style_grouped_rows():
    cli = _make_cli(tool_progress="compact")

    cli._on_thinking("Thinking")
    cli._on_tool_progress("tool.started", "terminal", "git status", {"command": "git status"})
    cli._on_tool_progress("tool.started", "terminal", "git diff", {"command": "git diff"})
    cli._on_tool_progress("tool.started", "mem0_search", "memory", {"query": "foo"})

    assert cli._spinner_text == "☁️×1 Thinking\n🌿×2 Using git\n🧠×1 Reading memory"
    assert cli._get_spinner_display_text() == "  ☁️×1 Thinking\n  🌿×2 Using git\n  🧠×1 Reading memory"


def test_compact_progress_synthesizes_thinking_before_first_tool():
    cli = _make_cli(tool_progress="compact")

    cli._on_tool_progress("tool.started", "browser_navigate", "https://example.com", {"url": "https://example.com"})

    assert cli._spinner_text == "☁️×1 Thinking\n🖱️×1 Using browser"


def test_compact_progress_hides_thinking_row_when_reasoning_style_is_hidden():
    cli = _make_cli(tool_progress="compact", reasoning_style="hidden")

    cli._on_thinking("Thinking")
    cli._on_tool_progress("tool.started", "browser_navigate", "https://example.com", {"url": "https://example.com"})

    assert cli._spinner_text == "🖱️×1 Using browser"


def test_compact_spinner_height_counts_rendered_lines():
    cli = _make_cli(tool_progress="compact")

    cli._on_thinking("Thinking")
    cli._on_tool_progress("tool.started", "terminal", "git status", {"command": "git status"})
    cli._on_tool_progress("tool.started", "session_search", "memory", {"query": "foo"})

    assert cli._spinner_widget_height(width=80) == 3


def test_compact_spinner_height_uses_display_width_not_python_len():
    cli = _make_cli(tool_progress="compact")

    cli._spinner_text = "☁️×1 Thinking"
    cli._status_bar_display_width = lambda text: 140 if "Thinking" in text else len(text)

    assert cli._spinner_widget_height(width=80) == 2


def test_compact_chat_resets_state_for_direct_callers():
    cli = _make_cli(tool_progress="compact")
    cli._ensure_runtime_credentials = lambda: True
    cli._resolve_turn_agent_config = lambda message: {
        "signature": cli._active_agent_route_signature,
        "model": None,
        "runtime": None,
        "label": None,
        "request_overrides": None,
    }
    cli._init_agent = lambda **kwargs: True
    cli.agent = SimpleNamespace(
        run_conversation=lambda **kwargs: {
            "final_response": "done",
            "messages": [{"role": "user", "content": kwargs["user_message"]}, {"role": "assistant", "content": "done"}],
            "completed": True,
        },
        max_iterations=90,
        interrupt=lambda msg: None,
        _active_children=[],
        _interrupt_requested=False,
        _summarize_api_error=lambda exc: str(exc),
    )

    cli._on_tool_progress("tool.started", "terminal", "git status", {"command": "git status"})
    assert cli._compact_progress_counts

    response = cli.chat("hello")

    assert response == "done"
    assert cli._spinner_text == ""
    assert not cli._compact_progress_counts
    assert cli._compact_thinking_seen is False


def test_compact_progress_prints_kept_scrollback_summary_once():
    cli = _make_cli(tool_progress="compact")

    cli._on_thinking("Thinking")
    cli._on_tool_progress("tool.started", "terminal", "git status", {"command": "git status"})

    cprint_globals = cli._print_compact_progress_scrollback_once.__globals__
    mock_print = MagicMock()
    with patch.dict(cprint_globals, {"_cprint": mock_print}):
        cli._print_compact_progress_scrollback_once()
        cli._print_compact_progress_scrollback_once()

    mock_print.assert_any_call("  ☁️×1 Thinking")
    mock_print.assert_any_call("  🌿×1 Using git")
    assert mock_print.call_count == 2


def test_compact_progress_does_not_print_thinking_only_summary():
    cli = _make_cli(tool_progress="compact")

    cli._on_thinking("Thinking")

    with patch("cli._cprint") as mock_print:
        cli._print_compact_progress_scrollback_once()

    mock_print.assert_not_called()


def test_single_line_compact_layout_matches_gateway_separator():
    cli = _make_cli(tool_progress="compact", compact_progress_layout="single_line")

    cli._on_thinking("Thinking")
    cli._on_tool_progress("tool.started", "terminal", "git status", {"command": "git status"})
    cli._on_tool_progress("tool.started", "delegate_task", "subagent", {})

    assert cli._spinner_text == "☁️×1 Thinking · 🌿×1 Using git · 🤖×1 Running agents"


def test_compact_progress_classifies_terminal_commands():
    from agent.display import group_compact_progress_tool, render_compact_tool_progress
    from collections import OrderedDict

    commands = {
        "git status": "git",
        "pytest tests -q": "tests",
        "gh issue list": "github",
        "op item list": "onepassword",
        "ssh mini uptime": "ssh",
        "launchctl print gui/501/foo": "launchctl",
        "brew update": "homebrew",
        "pnpm test": "packages",
        "uv run python script.py": "python",
        "printf hello": "terminal",
    }
    buckets = [group_compact_progress_tool("terminal", {"command": cmd}) for cmd in commands]
    assert buckets == list(commands.values())
    rendered = render_compact_tool_progress(OrderedDict((bucket, 1) for bucket in buckets))
    assert "🌿×1 Using git" in rendered
    assert "🧪×1 Running tests" in rendered
    assert "🐙×1 Using GitHub" in rendered
    assert "🔑×1 Using 1Password" in rendered
    assert "💻×1 SSH session" in rendered
    assert "🚀×1 Using launchctl" in rendered
    assert "🍺×1 Using Homebrew" in rendered
    assert "📦×1 Using npm/pnpm" in rendered
    assert "🐍×1 Using Python" in rendered
    assert "💻×1 Running terminal" in rendered


def test_compact_progress_classifies_external_agent_wrappers_without_false_mentions():
    from agent.display import group_compact_progress_tool, render_compact_tool_progress
    from collections import OrderedDict

    commands = {
        "claude --permission-mode auto --prefill 'fix it'": "claude_code",
        "/Users/Kosta/.local/bin/claude --print 'review'": "claude_code",
        "script -q /tmp/claude-batch.typescript claude --print --permission-mode bypassPermissions 'task'": "claude_code",
        "script -q /tmp/out.typescript /Users/Kosta/.local/bin/claude --print 'task'": "claude_code",
        "bash -lc 'claude --print \\\"task\\\"'": "claude_code",
        "python /tmp/run_claude_lane.py": "claude_code",
        "/Users/Kosta/.hermes/claude-telegram/start.sh": "claude_code",
        "agy -p 'task'": "antigravity",
        "/Users/Kosta/.local/bin/agy --add-dir . -p 'task'": "antigravity",
        "bash -lc 'agy --continue -p \\\"next\\\"'": "antigravity",
        "python /tmp/run_antigravity_lane.py": "antigravity",
        "tmux new-session -d -s claude-hermes-remote -c /Users/Kosta/.hermes export PATH='/x:/y'; exec '/Users/Kosta/.local/bin/claude' --remote-control --name Hermes": "claude_code",
        "tmux new-session -d -s agy exec '/usr/local/bin/antigravity' run": "antigravity",
        "screen -dmS cc /Users/Kosta/.local/bin/claude --foo": "claude_code",
        "grep claude README.md": "terminal",
        "cat ~/.claude.json": "terminal",
    }

    for command, expected in commands.items():
        assert group_compact_progress_tool("terminal", {"command": command}) == expected

    rendered = render_compact_tool_progress(OrderedDict([
        ("claude_code", 1),
        ("antigravity", 1),
    ]))
    assert "☀️×1 Running Claude Code" in rendered
    assert "✨×1 Running Antigravity" in rendered
