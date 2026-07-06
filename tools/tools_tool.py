"""Read-only tool-surface discovery helper.

This compact ``tools(action=...)`` surface gives the model a way to discover
visible and advanced tool groups without exposing every advanced schema by
default. It is intentionally read-only: it reports the effective launch-time
configuration and registered schemas, but it does not mutate a live session's
tool set.
"""

from __future__ import annotations

import json
import os
from typing import Any

from tools.registry import registry, tool_error


_ACTIONS = {"list", "describe", "help", "config"}
_REDACTED_KEY_MARKERS = ("key", "token", "secret", "password", "credential")
_DEFAULT_PLATFORM = "telegram"


_TOOL_HELP = {
    "tools": {
        "summary": "Read-only discovery for visible and advanced tool surfaces.",
        "examples": [
            {"action": "list"},
            {"action": "list", "category": "browser"},
            {"action": "describe", "name": "fs"},
            {"action": "config"},
        ],
    },
    "fs": {
        "summary": "File reads, writes, patches, and searches through one compact wrapper.",
        "fallbacks": "No shell fallback is automatic. Use terminal only when the file tool explicitly cannot express the operation.",
        "examples": [
            {"action": "read", "path": "README.md", "offset": 1, "limit": 80},
            {"action": "search", "pattern": "TODO", "path": "."},
            {"action": "patch", "mode": "replace", "path": "app.py", "old_string": "old", "new_string": "new"},
        ],
    },
    "web": {
        "summary": "Web search and page extraction through the configured Exa/Firecrawl/curl.md backends.",
        "fallbacks": "Fetch fallbacks must be explicit in results. Search results are never represented as fetched page content.",
        "examples": [
            {"action": "search", "query": "Hermes Agent docs", "limit": 5},
            {"action": "fetch", "urls": ["https://example.com"]},
            {"action": "answer", "urls": ["https://example.com"], "question": "What is this page about?"},
            {"action": "curlmd", "url": "https://example.com"},
        ],
    },
    "browser": {
        "summary": "Browser automation remains separate from macOS computer_use. Debug tools may move behind progressive disclosure later.",
        "examples": [
            {"category": "browser"},
        ],
    },
}


_CATEGORIES = {
    "browser": {
        "description": "Browser automation via agent-browser/CDP. Keep separate from computer_use.",
        "default_tools": [
            "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
            "browser_scroll", "browser_back", "browser_press", "browser_get_images",
        ],
        "advanced_tools": ["browser_vision", "browser_console", "browser_cdp", "browser_dialog"],
    },
    "file": {
        "description": "File read/write/patch/search operations.",
        "default_tools": ["fs"],
        "advanced_tools": ["read_file", "write_file", "patch", "search_files"],
    },
    "web": {
        "description": "Web search, extraction, page QA, summaries, structured extraction, and curl.md fallback.",
        "default_tools": ["web", "web_search", "github_repo_brief"],
        "advanced_tools": ["web_extract", "curlmd_fetch"],
    },
    "shell": {
        "description": "Local code/shell execution with separate safety boundaries.",
        "default_tools": ["terminal", "process", "execute_code"],
        "advanced_tools": [],
    },
    "memory": {
        "description": "Session planning, durable memory, and transcript search.",
        "default_tools": ["todo", "memory", "session_search"],
        "advanced_tools": [],
    },
    "media": {
        "description": "Vision, image/video generation, image processing, and TTS. Intentionally not merged.",
        "default_tools": ["vision_analyze", "image_generate", "image_process", "video_generate", "text_to_speech"],
        "advanced_tools": [],
    },
}


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _safe_config_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if any(marker in key_s.lower() for marker in _REDACTED_KEY_MARKERS):
                safe[key_s] = bool(str(item).strip()) if item is not None else False
            else:
                safe[key_s] = _safe_config_value(item)
        return safe
    if isinstance(value, list):
        return [_safe_config_value(item) for item in value]
    return value


def _schema_summary(name: str) -> dict[str, Any] | None:
    schema = registry.get_schema(name)
    entry = registry.get_entry(name)
    if not schema and not entry:
        return None
    params = (schema or {}).get("parameters", {})
    properties = params.get("properties", {}) if isinstance(params, dict) else {}
    return {
        "name": name,
        "toolset": entry.toolset if entry else None,
        "description": (schema or {}).get("description") or (entry.description if entry else ""),
        "required": params.get("required", []) if isinstance(params, dict) else [],
        "parameters": {
            key: {
                "type": value.get("type") or ("oneOf" if "oneOf" in value else None),
                "enum": value.get("enum"),
                "description": value.get("description"),
            }
            for key, value in properties.items()
            if isinstance(value, dict)
        },
    }


def _tool_visibility(name: str, visible_names: set[str]) -> str:
    if name in visible_names:
        return "visible"
    if registry.get_entry(name):
        return "advanced"
    return "planned"


def _resolve_platform(platform: Any = None) -> tuple[str, list[str], set[str]]:
    platform_name = str(platform or os.environ.get("HERMES_PLATFORM") or _DEFAULT_PLATFORM).strip() or _DEFAULT_PLATFORM
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        from model_tools import get_tool_definitions

        cfg = load_config()
        enabled_toolsets = sorted(_get_platform_tools(cfg, platform_name))
        visible = {
            td.get("function", {}).get("name")
            for td in get_tool_definitions(enabled_toolsets=enabled_toolsets, quiet_mode=True)
        }
        visible.discard(None)
        return platform_name, enabled_toolsets, set(visible)
    except Exception:
        return platform_name, [], set()


def _list_tools(category: Any = None, platform: Any = None) -> str:
    platform_name, enabled_toolsets, visible = _resolve_platform(platform)
    categories = _CATEGORIES
    if category:
        key = str(category).strip().lower()
        categories = {key: _CATEGORIES[key]} if key in _CATEGORIES else {}
        if not categories:
            return tool_error(f"Unknown category '{category}'. Known categories: {', '.join(sorted(_CATEGORIES))}")

    result = {
        "platform": platform_name,
        "enabled_toolsets": enabled_toolsets,
        "categories": {},
    }
    for key, spec in categories.items():
        default_tools = [
            {"name": name, "visibility": _tool_visibility(name, visible)}
            for name in spec["default_tools"]
        ]
        advanced_tools = [
            {"name": name, "visibility": _tool_visibility(name, visible)}
            for name in spec["advanced_tools"]
        ]
        result["categories"][key] = {
            "description": spec["description"],
            "default_tools": default_tools,
            "advanced_tools": advanced_tools,
        }
    return _json(result)


def _describe_tool(name: Any) -> str:
    if not name:
        return tool_error("name is required for action='describe'.")
    summary = _schema_summary(str(name))
    if summary is None:
        return tool_error(f"Tool '{name}' is not registered or planned in this runtime.")
    return _json(summary)


def _help_tool(name: Any = None) -> str:
    if not name:
        return _json({"available": sorted(_TOOL_HELP), "hint": "Pass name to get focused help."})
    key = str(name).strip()
    info = _TOOL_HELP.get(key)
    if info is None:
        summary = _schema_summary(key)
        if summary is None:
            return tool_error(f"No help found for '{name}'.")
        info = {"summary": summary.get("description"), "schema": summary}
    return _json({"name": key, **info})


def _config(platform: Any = None) -> str:
    platform_name, enabled_toolsets, visible = _resolve_platform(platform)
    try:
        from hermes_cli.config import load_config, get_config_path
        cfg = load_config()
        config_path = str(get_config_path())
    except Exception:
        cfg = {}
        config_path = None

    relevant = {
        "profile": os.environ.get("HERMES_PROFILE") or "default",
        "platform": platform_name,
        "config_path": config_path,
        "enabled_toolsets": enabled_toolsets,
        "visible_tool_count": len(visible),
        "web": _safe_config_value((cfg or {}).get("web", {})),
        "browser": _safe_config_value((cfg or {}).get("browser", {})),
        "platform_toolsets": _safe_config_value((cfg or {}).get("platform_toolsets", {})),
        "agent_disabled_toolsets": _safe_config_value(((cfg or {}).get("agent") or {}).get("disabled_toolsets", [])),
    }
    return _json(relevant)


def tools(action: str, name: Any = None, category: Any = None, platform: Any = None) -> str:
    """Read-only discovery router for Hermes tool surfaces."""
    if action not in _ACTIONS:
        return tool_error("Unknown action. Use one of: list, describe, help, config.")
    if action == "list":
        return _list_tools(category=category, platform=platform)
    if action == "describe":
        return _describe_tool(name)
    if action == "help":
        return _help_tool(name)
    return _config(platform=platform)


TOOLS_SCHEMA = {
    "name": "tools",
    "description": (
        "Read-only discovery for Hermes tool surfaces. Use this to list visible/default "
        "and advanced tools by category, inspect compact wrapper schemas, get usage help, "
        "or view safe effective tool configuration. Does not enable/disable tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "describe", "help", "config"],
                "description": "Read-only discovery action to perform.",
            },
            "name": {
                "type": "string",
                "description": "Tool or wrapper name for describe/help, e.g. fs, web, browser_navigate.",
            },
            "category": {
                "type": "string",
                "enum": sorted(_CATEGORIES),
                "description": "Optional category filter for action=list.",
            },
            "platform": {
                "type": "string",
                "description": "Optional platform key for config/list, e.g. telegram, discord, cli.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="tools",
    toolset="tools",
    schema=TOOLS_SCHEMA,
    handler=lambda args, **kw: tools(
        action=args.get("action", ""),
        name=args.get("name"),
        category=args.get("category"),
        platform=args.get("platform"),
    ),
    emoji="🧰",
    max_result_size_chars=50_000,
)
