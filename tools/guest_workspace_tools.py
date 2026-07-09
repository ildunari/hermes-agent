"""Guest workspace tools.

These wrappers intentionally expose a smaller file surface than the normal fs
and terminal tools.  They are rooted at HERMES_GUEST_SANDBOX_ROOT and refuse
symlink/path escapes before doing any work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gateway.guest_access import default_guest_sandbox_root, resolve_under_sandbox
from tools.registry import registry


MAX_READ_CHARS = 50_000


def _root() -> Path:
    root = default_guest_sandbox_root().expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve(path_value: str | None) -> Path:
    resolved = resolve_under_sandbox(path_value or ".", _root())
    if resolved is None:
        raise ValueError(f"path must stay inside guest sandbox {_root()}")
    return resolved


def guest_fs(action: str = "list", path: str = ".", content: str | None = None, pattern: str | None = None, limit: int = 100) -> str:
    """Read/write/list/search files inside the guest sandbox only."""
    try:
        target = _resolve(path)
        action = (action or "list").strip().lower()
        if action == "list":
            if not target.exists():
                return json.dumps({"error": "path not found", "path": str(target)})
            if target.is_file():
                return json.dumps({"files": [str(target.relative_to(_root()))]})
            files = []
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower())[: max(1, min(limit, 1000))]:
                files.append(("dir/" if child.is_dir() else "file/") + str(child.relative_to(_root())))
            return json.dumps({"root": str(_root()), "files": files}, ensure_ascii=False)
        if action == "read":
            if not target.is_file():
                return json.dumps({"error": "not a file", "path": str(target)})
            text = target.read_text(encoding="utf-8", errors="replace")
            truncated = len(text) > MAX_READ_CHARS
            return json.dumps({"path": str(target.relative_to(_root())), "content": text[:MAX_READ_CHARS], "truncated": truncated}, ensure_ascii=False)
        if action == "write":
            if content is None:
                return json.dumps({"error": "content is required for write"})
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return json.dumps({"ok": True, "path": str(target.relative_to(_root())), "bytes": len(content.encode("utf-8"))})
        if action == "search":
            needle = pattern or ""
            if not needle:
                return json.dumps({"error": "pattern is required for search"})
            base = target if target.is_dir() else target.parent
            matches: list[dict[str, Any]] = []
            for file in base.rglob("*"):
                if len(matches) >= max(1, min(limit, 1000)):
                    break
                try:
                    if not file.is_file():
                        continue
                    file.resolve().relative_to(_root())
                    text = file.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for idx, line in enumerate(text.splitlines(), 1):
                    if needle.lower() in line.lower():
                        matches.append({"path": str(file.relative_to(_root())), "line": idx, "text": line[:500]})
                        break
            return json.dumps({"matches": matches}, ensure_ascii=False)
        return json.dumps({"error": f"unsupported action: {action}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


GUEST_FS_SCHEMA = {
    "name": "guest_fs",
    "description": "List, read, write, or search files inside the guest sandbox only. Cannot access Kosta's home/profile files outside the sandbox.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "read", "write", "search"], "default": "list"},
            "path": {"type": "string", "description": "Path relative to the guest sandbox, or an absolute path under it."},
            "content": {"type": "string", "description": "Content for write action."},
            "pattern": {"type": "string", "description": "Plain text pattern for search action."},
            "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
        },
    },
}


def _dispatch_guest_fs(args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
    args = args or {}
    return guest_fs(
        action=args.get("action", "list"),
        path=args.get("path", "."),
        content=args.get("content"),
        pattern=args.get("pattern"),
        limit=args.get("limit", 100),
    )


registry.register(
    name="guest_fs",
    toolset="guest_file",
    schema=GUEST_FS_SCHEMA,
    handler=_dispatch_guest_fs,
    description="Sandboxed guest file operations",
    emoji="📁",
)
