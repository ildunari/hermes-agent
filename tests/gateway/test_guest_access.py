"""Tests for BlueBubbles family guest routing and guest tool policy."""

import json

from gateway.config import Platform
from gateway.guest_access import (
    ContactRegistry,
    GuestRoute,
    approved_bluebubbles_contacts_in_message,
    classify_bluebubbles_route,
    evaluate_guest_tool_call,
    normalize_identity,
)
from gateway.session import SessionSource
from model_tools import handle_function_call
from tools.guest_workspace_tools import guest_fs
from toolsets import resolve_toolset

OWNER_PHONE = "+12025550123"
GUEST_PHONE = "+12025550124"
UNKNOWN_PHONE = "+12025550999"


def _registry():
    return ContactRegistry.from_dict({
        "owner_profile": "poke",
        "owner_contact_id": "kosta-owner",
        "guest_profile": "guest",
        "owner_identities": [OWNER_PHONE, "kosta@example.com"],
        "contacts": {
            "stephen-lucier": {
                "display_name": "Steve Lucier",
                "role": "family_guest",
                "identities": {"bluebubbles": {"handles": [GUEST_PHONE]}},
                "allowed_surfaces": ["bluebubbles"],
            }
        },
    })


def _source(user_id, chat_type="dm", chat_id="chat"):
    return SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name=user_id,
    )


def test_normalize_bluebubbles_phone_and_email_aliases():
    assert normalize_identity("202-555-0123") == OWNER_PHONE
    assert normalize_identity("+1 (202) 555-0123") == OWNER_PHONE
    assert normalize_identity("iMessage;-;Kosta@Example.COM") == "kosta@example.com"


def test_owner_dm_routes_to_poke_profile():
    decision = classify_bluebubbles_route(_source("+1 202 555 0123"), {}, _registry())
    assert decision.route is GuestRoute.OWNER
    assert decision.profile == "poke"
    assert decision.contact_id == "kosta-owner"
    assert decision.reason == "owner sender"


def test_approved_family_dm_routes_to_guest_profile():
    decision = classify_bluebubbles_route(_source("202-555-0124"), {}, _registry())
    assert decision.route is GuestRoute.GUEST
    assert decision.profile == "guest"
    assert decision.contact_id == "stephen-lucier"
    assert decision.contact_display_name == "Steve Lucier"
    assert decision.contact_role == "family_guest"


def test_unknown_dm_is_denied_not_default_guest():
    decision = classify_bluebubbles_route(_source(UNKNOWN_PHONE), {}, _registry())
    assert decision.route is GuestRoute.DENY
    assert decision.profile is None


def test_group_with_owner_participant_keeps_guest_sender_on_guest_profile():
    raw = {
        "data": {
            "chats": [{
                "participants": [
                    {"address": OWNER_PHONE},
                    {"address": GUEST_PHONE},
                ]
            }]
        }
    }
    decision = classify_bluebubbles_route(
        _source(GUEST_PHONE, chat_type="group", chat_id="iMessage;+;group"),
        raw,
        _registry(),
    )
    assert decision.route is GuestRoute.GUEST
    assert decision.profile == "guest"
    assert decision.contact_id == "stephen-lucier"
    assert decision.contact_display_name == "Steve Lucier"
    assert decision.contact_role == "family_guest"


def test_group_without_owner_allows_approved_guest_sender():
    raw = {"data": {"chats": [{"participants": [{"address": GUEST_PHONE}]}]}}
    decision = classify_bluebubbles_route(
        _source(GUEST_PHONE, chat_type="group", chat_id="iMessage;+;group"),
        raw,
        _registry(),
    )
    assert decision.route is GuestRoute.GUEST
    assert decision.profile == "guest"
    assert decision.contact_id == "stephen-lucier"


def test_group_unknown_sender_is_denied_but_approved_contacts_are_discoverable():
    raw = {"data": {"chats": [{"participants": [{"address": GUEST_PHONE}, {"address": UNKNOWN_PHONE}]}]}}
    registry = _registry()
    decision = classify_bluebubbles_route(
        _source(UNKNOWN_PHONE, chat_type="group", chat_id="iMessage;+;group"),
        raw,
        registry,
    )
    assert decision.route is GuestRoute.DENY
    approved = approved_bluebubbles_contacts_in_message(raw, registry)
    assert [contact.contact_id for contact in approved] == ["stephen-lucier"]


def test_guest_toolset_excludes_admin_and_paid_tools():
    tools = set(resolve_toolset("hermes-bluebubbles-guest"))
    assert "video_generate" not in tools
    assert "send_message" not in tools
    assert "codex_subtask" not in tools
    assert "delegate_task" not in tools
    assert "computer_use" not in tools
    assert "cronjob" not in tools
    assert "session_search" not in tools
    assert "memory" not in tools
    assert "fs" not in tools
    assert "terminal" in tools
    assert "process" in tools
    assert "execute_code" in tools
    assert "browser_navigate" in tools
    assert "browser_click" in tools
    assert "guest_fs" in tools


def test_guest_tool_policy_blocks_paid_admin_and_host_capable_tools(tmp_path):
    root = tmp_path / "guest-workspace"
    root.mkdir()
    assert evaluate_guest_tool_call("video_generate", {}, root).requires_approval is True
    assert evaluate_guest_tool_call("send_message", {}, root).requires_approval is True
    assert evaluate_guest_tool_call("session_search", {}, root).allowed is False
    assert evaluate_guest_tool_call("fs", {"action": "read", "path": str(root / "ok.txt")}, root).allowed is False
    assert evaluate_guest_tool_call("terminal", {"command": "python -V", "workdir": str(root)}, root).allowed is True
    assert evaluate_guest_tool_call("execute_code", {"code": "print('hi')"}, root).allowed is True


def test_guest_terminal_policy_blocks_host_paths_and_1password(tmp_path):
    root = tmp_path / "guest-workspace"
    root.mkdir()
    assert evaluate_guest_tool_call("terminal", {"command": "python -V", "workdir": str(root)}, root).allowed is True
    assert evaluate_guest_tool_call("terminal", {"command": "op item list", "workdir": str(root)}, root).allowed is False
    assert evaluate_guest_tool_call("terminal", {"command": "cat ~/.hermes/config.yaml", "workdir": str(root)}, root).allowed is False
    assert evaluate_guest_tool_call("terminal", {"command": "pwd", "workdir": "/Users/Kosta"}, root).allowed is False


def test_guest_terminal_policy_blocks_absolute_write_outside_sandbox(tmp_path):
    """Live finding: workdir alone is not a sandbox -- a command can `cd`
    inside the sandbox and still copy to an absolute destination outside it
    (`cp <attachment> /tmp/x` previously succeeded)."""
    root = tmp_path / "guest-workspace"
    root.mkdir()
    attachment = root / "attachments" / "photo.jpg"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("fake image data")

    blocked = evaluate_guest_tool_call(
        "terminal",
        {"command": f"cp {attachment} /tmp/x", "workdir": str(root)},
        root,
    )
    assert blocked.allowed is False
    assert "outside the guest sandbox" in blocked.reason

    # Redirection-style escape (`>`, `>>`) must be caught the same way.
    blocked_redirect = evaluate_guest_tool_call(
        "terminal",
        {"command": f"cat {attachment} > /tmp/leak.jpg", "workdir": str(root)},
        root,
    )
    assert blocked_redirect.allowed is False


def test_guest_terminal_policy_allows_absolute_paths_inside_sandbox(tmp_path):
    """The fix must not regress normal in-sandbox absolute-path commands."""
    root = tmp_path / "guest-workspace"
    root.mkdir()
    src = root / "a.txt"
    src.write_text("hi")

    allowed = evaluate_guest_tool_call(
        "terminal",
        {"command": f"cp {src} {root}/b.txt", "workdir": str(root)},
        root,
    )
    assert allowed.allowed is True


def test_guest_terminal_policy_allowlists_readonly_system_bin_paths(tmp_path):
    """Normal commands invoking a binary by absolute path (env, homebrew
    tools, etc) must not be broken by the new absolute-path guard."""
    root = tmp_path / "guest-workspace"
    root.mkdir()

    allowed = evaluate_guest_tool_call(
        "terminal",
        {"command": "/usr/bin/env python3 -c 'print(1)'", "workdir": str(root)},
        root,
    )
    assert allowed.allowed is True


def test_guest_terminal_policy_does_not_flag_urls_as_path_escapes(tmp_path):
    """A URL argument (curl, wget) must not be misread as a filesystem path
    escape just because it contains '/' after the scheme."""
    root = tmp_path / "guest-workspace"
    root.mkdir()

    allowed = evaluate_guest_tool_call(
        "terminal",
        {"command": "curl -s https://example.com/api/data", "workdir": str(root)},
        root,
    )
    assert allowed.allowed is True


def test_guest_execute_code_policy_blocks_absolute_write_outside_sandbox(tmp_path):
    """Same write-escape class for execute_code: a script can write outside
    the sandbox without tripping the sensitive-marker or host-home checks."""
    root = tmp_path / "guest-workspace"
    root.mkdir()

    blocked = evaluate_guest_tool_call(
        "execute_code",
        {"code": "open('/tmp/x', 'w').write('leak')"},
        root,
    )
    assert blocked.allowed is False
    assert "outside the guest sandbox" in blocked.reason


def test_guest_execute_code_policy_allows_writes_inside_sandbox(tmp_path):
    root = tmp_path / "guest-workspace"
    root.mkdir()
    target = root / "ok.txt"

    allowed = evaluate_guest_tool_call(
        "execute_code",
        {"code": f"open({str(target)!r}, 'w').write('fine')"},
        root,
    )
    assert allowed.allowed is True


def test_guest_policy_blocks_host_home_from_path_home(tmp_path):
    """Guest denylist must match Path.home(), not a hardcoded Studio path."""
    from pathlib import Path as P
    root = tmp_path / "guest-workspace"
    root.mkdir()
    home = str(P.home())
    # Use a home path that is NOT covered by static _SENSITIVE_PATH_MARKERS so
    # the Path.home()-based arm is the one that fires.
    blocked_cat = evaluate_guest_tool_call(
        "terminal",
        {"command": f"cat {home}/Desktop/notes.txt", "workdir": str(root)},
        root,
    )
    assert blocked_cat.allowed is False
    blocked_code = evaluate_guest_tool_call(
        "execute_code",
        {"code": f"open({home!r} + '/Desktop/notes.txt').read()"},
        root,
    )
    assert blocked_code.allowed is False
    assert "host home" in blocked_code.reason




def test_guest_fs_reads_and_writes_only_inside_sandbox(monkeypatch, tmp_path):
    root = tmp_path / "guest-workspace"
    monkeypatch.setenv("HERMES_GUEST_SANDBOX_ROOT", str(root))
    written = json.loads(guest_fs(action="write", path="notes/plan.md", content="hello family"))
    assert written["ok"] is True
    read = json.loads(guest_fs(action="read", path="notes/plan.md"))
    assert read["content"] == "hello family"
    denied = json.loads(guest_fs(action="read", path="/Users/Kosta/.hermes/config.yaml"))
    assert "guest sandbox" in denied["error"]


def test_guest_fs_blocks_symlink_escape(monkeypatch, tmp_path):
    root = tmp_path / "guest-workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    monkeypatch.setenv("HERMES_GUEST_SANDBOX_ROOT", str(root))
    denied = json.loads(guest_fs(action="read", path="link.txt"))
    assert "guest sandbox" in denied["error"]




def test_guest_web_policy_blocks_private_fetch_urls():
    public = evaluate_guest_tool_call("web", {"action": "fetch", "urls": ["https://example.com"]})
    assert public.allowed is True
    private = evaluate_guest_tool_call("web", {"action": "fetch", "urls": ["http://127.0.0.1:1338/v1"]})
    assert private.allowed is False
    assert "private-network" in private.reason
    local = evaluate_guest_tool_call("web", {"action": "curlmd", "url": "http://mac-mini.local:1234"})
    assert local.allowed is False


def test_guest_policy_blocks_browser_private_url_bypass():
    blocked = evaluate_guest_tool_call("browser_navigate", {"url": "http://127.0.0.1:8645"})
    assert blocked.allowed is False
    assert blocked.requires_approval is True


def test_guest_fs_dispatches_through_registry(monkeypatch, tmp_path):
    from tools.registry import registry
    import tools.guest_workspace_tools  # noqa: F401 - ensures registration

    root = tmp_path / "guest-workspace"
    monkeypatch.setenv("HERMES_GUEST_SANDBOX_ROOT", str(root))
    result = json.loads(registry.dispatch("guest_fs", {"action": "write", "path": "hello.txt", "content": "hi"}, task_id="t1"))
    assert result["ok"] is True
    read = json.loads(registry.dispatch("guest_fs", {"action": "read", "path": "hello.txt"}, task_id="t1"))
    assert read["content"] == "hi"


def test_guest_policy_guard_fails_closed_before_tool_dispatch(monkeypatch, tmp_path):
    root = tmp_path / "guest-workspace"
    root.mkdir()
    monkeypatch.setenv("HERMES_GUEST_POLICY", "1")
    monkeypatch.setenv("HERMES_GUEST_SANDBOX_ROOT", str(root))
    result = json.loads(handle_function_call("send_message", {"target": "telegram", "message": "hi"}))
    assert result["guest_policy"] is True
    assert result["requires_approval"] is True
