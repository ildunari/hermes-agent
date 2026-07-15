#!/usr/bin/env python3
"""Fail fast when a smart update drops known local carry invariants."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    path: str
    needles: tuple[str, ...]
    description: str


CHECKS = (
    Check("hermes_cli/web_server.py", ("register_instance", "allowed_hosts", "_configured_dashboard_allowed_hosts"), "dashboard callback and host allowlist"),
    Check("web/src/lib/internal-agent-input.ts", (), "internal agent input helper"),
    Check("web/src/components/ProviderUsagePanel.tsx", (), "provider usage component"),
    Check("web/src/App.tsx", ("ProviderUsagePanel",), "provider usage mount"),
    Check("web/src/lib/api.ts", ("subscription-usage",), "subscription usage API client"),
    Check("tools/send_message_tool.py", ("rich_card", "bluebubbles"), "rich-card and BlueBubbles delivery"),
    Check("tools/mcp_tool.py", ("session is None", "skipping dynamic tool refresh; session is closed"), "closed-session MCP refresh guard"),
    Check("hermes_state.py", ("_fts_trigram_disabled", "disable_fts_trigram", "last_active", "idx_sessions_last_active"), "trigram-disable lifecycle and indexed session recents"),
    Check("cli.py", ("restart-gateways", "enqueue_detached_restart"), "CLI detached restart dispatch"),
    Check("gateway/run.py", ("_handle_detached_surface_restart_command", "__compact__", "compact_tool_counts", "_update_compact_tool_progress", "register_live_voice_streamer"), "gateway restart, compact HUD, and Discord live voice integration"),
    Check("plugins/platforms/discord/adapter.py", ("DiscordVoiceReplyStreamer", "_handle_barge_in_event", "start_busy_voice"), "Discord live voice, barge-in, and busy-audio lifecycle"),
    Check("hermes_cli/web_server.py", ("prepare_spoken_text", "spoken_formatter"), "feature-gated desktop spoken-text formatting"),
    Check("apps/desktop/electron/main.ts", ("autoplay-policy", "no-user-gesture-required"), "asynchronous Desktop read-aloud playback"),
    Check("hermes_cli/restart_surfaces.py", ("_resolve_loaded_service", "restarted_services", "_wait_for_scope_health"), "launchd domain dedupe and readiness polling"),
    Check("apps/desktop/package.json", ('"afterPack"', '"entitlements"'), "Desktop lifecycle hook declarations"),
    Check(
        "apps/desktop/scripts/after-pack.mjs",
        (
            "3A22F53A48A189F4A8766CACE00192860CC37F8F",
            "HERMES_OP_SHIM",
            "HERMES_SIGNING_PASSWORD_SERVICE",
            "serviceAccountToken",
            "set-key-partition-list",
            "apple-tool:,apple:,codesign:",
            "refusing to produce an ad-hoc local build",
        ),
        "unattended Developer ID signing",
    ),
    Check(
        "agent/context_compressor.py",
        ("_REPLAY_BUDGET_KEYS", "_serialized_length_for_budget"),
        "upstream replay-budget accounting for compaction tail",
    ),
    Check(
        "gateway/guest_access.py",
        ("_HOST_HOME_PATH", "_HOST_HOME_PATH_RE", "Path.home()"),
        "portable guest host-home denylist",
    ),
    Check(
        "apps/desktop/src/i18n/local-carry.ts",
        ("draftPendingNotice",),
        "draft pending notice outside hot locale literals",
    ),
    Check(
        "scripts/setup_local_merge_aids.sh",
        ("rerere.autoupdate", "git-rr-cache"),
        "shareable rerere setup helper",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    failures: list[str] = []
    for check in CHECKS:
        path = root / check.path
        if not path.is_file():
            failures.append(f"MISSING FILE: {check.path} ({check.description})")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in check.needles:
            if needle.lower() not in text.lower():
                failures.append(f"MISSING SYMBOL: {check.path}: {needle!r} ({check.description})")
    if failures:
        print("Local carry contract: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Local carry contract: PASS ({len(CHECKS)} connected surfaces checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
