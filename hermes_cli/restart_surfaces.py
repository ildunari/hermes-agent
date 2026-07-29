"""Detached Hermes surface restart helper.

This module exists so Telegram/Discord/CLI slash commands can hand off a
restart to a process that is not owned by the gateway being restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import enum
import json
import os
import plistlib
import pwd
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


LOG_PATH = Path.home() / ".hermes" / "logs" / "restart-surfaces.log"
LAUNCHCTL_BIN = "/bin/launchctl"
SUDO_BIN = "/usr/bin/sudo"
VISUDO_BIN = "/usr/sbin/visudo"
SUDOERS_DROPIN = Path("/private/etc/sudoers.d/hermes-restart-surfaces")
USER_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SYSTEM_LAUNCH_DAEMONS_DIR = Path("/Library/LaunchDaemons")


class BootstrapPolicy(enum.Enum):
    NEVER = "never"
    MULTIPLEX_CONFIGURED = "multiplex_configured"


@dataclass(frozen=True)
class RestartTarget:
    """One launchd target that can be restarted by the helper."""

    domain_template: str
    label: str
    required: bool = True
    description: str = ""
    bootstrap_policy: BootstrapPolicy = BootstrapPolicy.NEVER

    def domain(self, uid: int) -> str:
        return self.domain_template.format(uid=uid)

    def service_name(self, uid: int) -> str:
        return f"{self.domain(uid)}/{self.label}"


class RestartVerification(enum.Enum):
    """Outcome of proving that a graceful launchd restart completed."""

    RESTARTED = "restarted"
    NOT_RESTARTED = "not_restarted"
    UNVERIFIABLE = "unverifiable"


WEBUI_TARGETS: tuple[RestartTarget, ...] = (
    RestartTarget(
        "user/{uid}",
        "ai.hermes.webui",
        description="Hermes WebUI/dashboard",
        bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
    ),
)


GATEWAY_TARGETS: tuple[RestartTarget, ...] = (
    RestartTarget(
        "user/{uid}",
        "ai.hermes.gateway",
        description="default Hermes gateway",
        bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
    ),
    RestartTarget("user/{uid}", "ai.hermes.gateway-gpt", description="GPT Hermes gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-coding", required=False, description="coding profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-email-assistant", required=False, description="email-assistant profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-browser-agent", required=False, description="browser-agent profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-design", required=False, description="design profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-bookie", required=False, description="bookie profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-scientist", required=False, description="scientist profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-poke", required=False, description="Poke/BlueBubbles profile gateway"),
    # Some profile LaunchAgents with LimitLoadToSessionType Aqua/Background load
    # into the gui domain instead of user. Keep both optional targets so a full
    # restart touches whichever domain launchd actually chose.
    RestartTarget("gui/{uid}", "ai.hermes.gateway-design", required=False, description="design profile gateway"),
    RestartTarget("gui/{uid}", "ai.hermes.gateway-bookie", required=False, description="bookie profile gateway"),
    RestartTarget("gui/{uid}", "ai.hermes.gateway-scientist", required=False, description="scientist profile gateway"),
    RestartTarget("gui/{uid}", "ai.hermes.gateway-poke", required=False, description="Poke/BlueBubbles profile gateway"),
    # The WebUI/dashboard LaunchAgent owns the local dashboard backend on 9119.
    # It must move with /restart-gateways after smart updates; otherwise the
    # gateways can restart on new code while the dashboard keeps an old process.
    *WEBUI_TARGETS,
    RestartTarget(
        "system",
        "com.kosta.hermes-dashboard-system",
        required=False,
        description="dashboard backend on 9119",
        bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
    ),
    RestartTarget(
        "system",
        "com.kosta.hermes-dashboard-proxy-system",
        required=False,
        description="dashboard path proxy",
        bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
    ),
    RestartTarget(
        "user/{uid}",
        "ai.hermes.dashboard-host-rewrite-proxy",
        required=False,
        description="WebUI dashboard host rewrite proxy",
        bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
    ),
    # Dedicated remote dashboard backend for MacBook Hermes Desktop.
    RestartTarget(
        "user/{uid}",
        "ai.hermes.desktop-remote-dashboard",
        required=False,
        description="MacBook Hermes Desktop remote dashboard on 9120",
        bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
    ),
)

FULL_HERMES_TARGETS: tuple[RestartTarget, ...] = (
    *GATEWAY_TARGETS,
    RestartTarget("user/{uid}", "ai.hermes.workspace", required=False, description="Hermes Workspace"),
    RestartTarget("user/{uid}", "ai.hermes.workspace-proxy", required=False, description="Hermes Workspace path proxy"),
    RestartTarget("user/{uid}", "ai.hermes.watchdog", required=False, description="Hermes watchdog"),
    RestartTarget("user/{uid}", "ai.hermes.codex-subtask-supervisor", required=False, description="Codex subtask supervisor"),
    RestartTarget("user/{uid}", "com.kosta.hermes-gpt-api-proxy", required=False, description="GPT API path proxy"),
    RestartTarget("user/{uid}", "com.kosta.clawket-hermes-gpt", required=False, description="Clawket Hermes GPT surface"),
    RestartTarget("user/{uid}", "com.kosta.hermes-side-bridge", required=False, description="Hermes side bridge"),
    RestartTarget("user/{uid}", "com.kosta.hermes-voice-bridge", required=False, description="Hermes voice bridge"),
    RestartTarget("user/{uid}", "com.kosta.claude-hermes-miniapp", required=False, description="legacy Claude Hermes miniapp"),
    RestartTarget("user/{uid}", "com.kosta.claude-hermes-remote", required=False, description="legacy Claude Hermes remote"),
    RestartTarget("user/{uid}", "com.kosta.claude-hermes-telegram", required=False, description="legacy Claude Hermes Telegram"),
    RestartTarget("user/{uid}", "com.kosta.claude-hermes-telegram-legacy", required=False, description="legacy Claude Hermes Telegram fallback"),
    RestartTarget("user/{uid}", "com.kosta.claude-hermes-telegram-supervisor.staging", required=False, description="staging Telegram supervisor"),
    RestartTarget("system", "com.kosta.hermes-voice-bridge-system", required=False, description="voice bridge"),
    RestartTarget("system", "com.kosta.hermes-workspace-system", required=False, description="workspace backend"),
    RestartTarget("system", "com.kosta.hermes-workspace-proxy-system", required=False, description="workspace path proxy"),
)

VERIFY_PORTS: dict[str, tuple[int, ...]] = {
    "webui": (8787,),
    "gateways": (8642, 8643, 8644, 8787, 9119, 9120),
    "hermes": (8642, 8643, 8644, 8776, 8787, 9119, 9120, 9192, 3192, 3100),
}

_NAMED_GATEWAY_LABEL_PREFIX = "ai.hermes.gateway-"
_MULTIPLEX_SURFACE_PORTS: dict[str, tuple[int, ...]] = {
    "webui": (8787,),
    "gateways": (8787, 9119, 9120),
    "hermes": (8776, 8787, 9119, 9120, 9192, 3192, 3100),
}
_PORT_BINDING_PLATFORM_PORTS: dict[str, tuple[str, int]] = {
    "webhook": ("port", 8644),
    "api_server": ("port", 8642),
    "msgraph_webhook": ("port", 8646),
    "feishu": ("webhook_port", 8765),
    "wecom_callback": ("port", 8645),
    "bluebubbles": ("webhook_port", 8645),
    "sms": ("webhook_port", 8080),
    "whatsapp_cloud": ("webhook_port", 8090),
    "line": ("port", 8646),
}
_PLATFORM_PORT_ENV: dict[str, str] = {
    "webhook": "WEBHOOK_PORT",
    "api_server": "API_SERVER_PORT",
    "msgraph_webhook": "MSGRAPH_WEBHOOK_PORT",
    "wecom_callback": "WECOM_CALLBACK_PORT",
    "bluebubbles": "BLUEBUBBLES_WEBHOOK_PORT",
    "whatsapp_cloud": "WHATSAPP_CLOUD_WEBHOOK_PORT",
}

# Ports served by best-effort (required=False) targets — chiefly the dashboard
# LaunchDaemon (9119) and the MacBook remote dashboard (9120). Their daemons use
# ThrottleInterval=30, so a first-launch miss can't retry for 30s and a cold
# HTTP handler can lag the listening socket. A slow best-effort dashboard must
# NOT fail an otherwise-healthy restart whose required surfaces (gateways +
# WebUI) are up — that false negative marked a clean restart as exit 1. Probe
# them, log a warning if they lag, but keep them out of the exit-code decision.
BEST_EFFORT_VERIFY_PORTS: frozenset[int] = frozenset({9119, 9120})

GATEWAY_STATUS_PATHS: dict[str, Path] = {
    "ai.hermes.gateway": Path.home() / ".hermes" / "gateway_state.json",
    "ai.hermes.gateway-gpt": Path.home() / ".hermes" / "profiles" / "gpt" / "gateway_state.json",
    "ai.hermes.gateway-coding": Path.home() / ".hermes" / "profiles" / "coding" / "gateway_state.json",
    "ai.hermes.gateway-email-assistant": Path.home() / ".hermes" / "profiles" / "email-assistant" / "gateway_state.json",
    "ai.hermes.gateway-browser-agent": Path.home() / ".hermes" / "profiles" / "browser-agent" / "gateway_state.json",
    "ai.hermes.gateway-design": Path.home() / ".hermes" / "profiles" / "design" / "gateway_state.json",
    "ai.hermes.gateway-bookie": Path.home() / ".hermes" / "profiles" / "bookie" / "gateway_state.json",
    "ai.hermes.gateway-scientist": Path.home() / ".hermes" / "profiles" / "scientist" / "gateway_state.json",
    "ai.hermes.gateway-poke": Path.home() / ".hermes" / "profiles" / "poke" / "gateway_state.json",
}
# The gateway's loop heartbeat (written by gateway.shutdown_watchdog) lives at
# <HERMES_HOME>/state/gateway.heartbeat, i.e. next to each target's
# gateway_state.json. It refreshes every ~30s while the loop is alive, so a
# record older than three intervals cannot vouch for the launchd PID.
_HEARTBEAT_RELATIVE = ("state", "gateway.heartbeat")
HEARTBEAT_FRESH_WINDOW_S = 90.0
# Listener port owned by the root gateway's API server. Used as a second
# liveness fallback when both gateway_state.json and the heartbeat are unusable.
GATEWAY_LISTENER_PORT = 8642
# gateway_state.json's active_agents only rewrites on turn/claim/release
# boundaries. In the ordinary case those now fire immediately (api_server and
# cron claim/release paths persist on every mutation — see
# gateway/platforms/api_server.py, cron/scheduler.py), so the file is
# reliably fresh. But a single long-running turn with no intermediate
# mutation, a persist that failed, or a foreign clobber can still leave it
# stale. Trusting a stale nonzero active_agents forever wedged a drain for
# hours in docs/local/UPDATE_INCIDENTS_20260723.md item 12. Once the file is
# older than this window, ``_gateway_busy_details`` cross-checks the
# independent loop heartbeat (refreshed every ~30s regardless of activity;
# see gateway/shutdown_watchdog.py) before trusting a nonzero count.
ACTIVE_AGENTS_TRUST_WINDOW_S = 120.0
# A queued restart should behave like a staged operation: if another Hermes
# session is still running, wait for it to drain instead of forcing Kosta to
# rerun the command manually.  Twenty-four hours keeps a stuck gateway from
# hiding a failed restart forever while covering normal long agent runs.
DEFAULT_SAFE_WAIT_TIMEOUT = 24 * 60 * 60
DEFAULT_SAFE_WAIT_INTERVAL = 2.0
DEFAULT_BOOTSTRAP_WAIT_TIMEOUT = 10.0
DEFAULT_BOOTSTRAP_WAIT_INTERVAL = 0.25
# The system dashboard LaunchDaemon uses ThrottleInterval=30. A failed first
# launch (for example, while an updated editable checkout is refreshing
# bytecode) cannot be retried before that interval expires, so readiness must
# cover at least one throttled retry plus normal dashboard startup.
DEFAULT_HEALTH_WAIT_TIMEOUT = 60.0
DEFAULT_HEALTH_WAIT_INTERVAL = 0.5

# WebUI busy probe: the WebUI (ai.hermes.webui) hosts live chat turns whose
# worker state dies with the process. /health reports `active_runs` (worker
# runs independent of SSE attachment), so a restart that includes the WebUI
# target must wait until no runs are in flight — same contract as the
# gateway active_agents drain check.
WEBUI_BUSY_LABELS = frozenset({"ai.hermes.webui"})
WEBUI_HEALTH_URL = "http://127.0.0.1:8787/health"
WEBUI_HEALTH_TIMEOUT = 3.0
DESKTOP_BUSY_LABELS = frozenset({"ai.hermes.desktop-remote-dashboard"})
DESKTOP_HEALTH_URL = "http://127.0.0.1:9120/health"
DESKTOP_HEALTH_MARKER = "hermes-dashboard-ok"


def _desktop_dashboard_is_running() -> bool:
    """Return whether the optional remote Desktop dashboard is listening."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 9120), timeout=0.5):
            return True
    except OSError:
        return False


class RestartError(RuntimeError):
    pass


def normalize_scope(scope: str) -> str:
    raw = (scope or "").strip().lower().replace("_", "-")
    if raw in {"webui", "web-ui", "restart-webui"}:
        return "webui"
    if raw in {"gateway", "gateways", "restart-gateways"}:
        return "gateways"
    if raw in {"hermes", "all", "surface", "surfaces", "restart-hermes"}:
        return "hermes"
    raise RestartError(f"Unknown restart scope: {scope!r}")


def _unique_targets(targets: Iterable[RestartTarget]) -> tuple[RestartTarget, ...]:
    """Return targets in order, dropping duplicate launchd service entries."""

    seen: set[tuple[str, str]] = set()
    unique: list[RestartTarget] = []
    for target in targets:
        key = (target.domain_template, target.label)
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return tuple(unique)


def _multiplex_gateway_config() -> Any | None:
    try:
        from hermes_cli import managed_scope
        from hermes_cli.config import load_config_readonly, read_raw_config
        from hermes_cli.profiles import get_profile_dir
        from utils import is_truthy_value

        # Restart topology belongs to the root gateway, not to whichever named
        # profile happened to invoke the helper. WebUI/Desktop services export a
        # profile-scoped HERMES_HOME; reading that path made multiplex installs
        # resurrect retired named gateways and verify retired ports.
        root_config_path = get_profile_dir("default") / "config.yaml"
        config = load_config_readonly(config_path=root_config_path)
        raw_config = managed_scope.apply_managed_overlay(
            read_raw_config(config_path=root_config_path)
        )
    except Exception as exc:
        _append_log(
            "restart topology config probe failed; preserving legacy profile targets "
            f"({type(exc).__name__})"
        )
        return None
    env_value = os.getenv("GATEWAY_MULTIPLEX_PROFILES", "").strip().lower()
    if env_value in {"1", "true", "yes", "on"}:
        multiplex = True
    elif env_value in {"0", "false", "no", "off"}:
        multiplex = False
    elif "multiplex_profiles" in raw_config:
        multiplex = is_truthy_value(raw_config.get("multiplex_profiles"))
    elif isinstance(raw_config.get("gateway"), dict) and "multiplex_profiles" in raw_config["gateway"]:
        multiplex = is_truthy_value(raw_config["gateway"].get("multiplex_profiles"))
    else:
        multiplex = is_truthy_value(config.get("multiplex_profiles"))
    return config if multiplex else None


def _targets_for_scope(
    scope: str,
    multiplex_config: Any | None,
) -> tuple[RestartTarget, ...]:
    normalized = normalize_scope(scope)
    if normalized == "webui":
        targets = WEBUI_TARGETS
    elif normalized == "gateways":
        targets = GATEWAY_TARGETS
    else:
        targets = FULL_HERMES_TARGETS
    if multiplex_config is not None:
        targets = tuple(
            target
            for target in targets
            if not target.label.startswith(_NAMED_GATEWAY_LABEL_PREFIX)
        )
    return _unique_targets(targets)


def targets_for_scope(scope: str) -> tuple[RestartTarget, ...]:
    return _targets_for_scope(scope, _multiplex_gateway_config())


def _multiplex_listener_ports(config: Any) -> tuple[int, ...]:
    from gateway.config import platform_binds_port

    ports: set[int] = set()
    platform_blocks: dict[str, dict[str, Any]] = {}
    gateway_section = config.get("gateway") if isinstance(config, dict) else None
    for source in (
        gateway_section.get("platforms") if isinstance(gateway_section, dict) else None,
        config.get("platforms") if isinstance(config, dict) else None,
    ):
        if not isinstance(source, dict):
            continue
        for platform_name, raw_block in source.items():
            if not isinstance(raw_block, dict):
                continue
            prior = platform_blocks.get(str(platform_name), {})
            prior_extra_raw = prior.get("extra")
            prior_extra: dict[str, Any] = (
                prior_extra_raw if isinstance(prior_extra_raw, dict) else {}
            )
            block_extra_raw = raw_block.get("extra")
            block_extra: dict[str, Any] = (
                block_extra_raw if isinstance(block_extra_raw, dict) else {}
            )
            merged_block: dict[str, Any] = dict(prior)
            merged_block.update({str(key): value for key, value in raw_block.items()})
            merged_extra: dict[str, Any] = dict(prior_extra)
            merged_extra.update(block_extra)
            merged_block["extra"] = merged_extra
            platform_blocks[str(platform_name)] = merged_block
    for platform_name, platform_config in platform_blocks.items():
        if not platform_config.get("enabled", False):
            continue
        platform_name = platform_name.strip().lower()
        port_spec = _PORT_BINDING_PLATFORM_PORTS.get(platform_name)
        extra = platform_config.get("extra")
        if port_spec is None or not isinstance(extra, dict):
            continue
        effective = {**extra, **platform_config}
        if not platform_binds_port(platform_name, effective):
            continue
        if platform_name == "bluebubbles" and effective.get("webhook_register", True) is False:
            continue
        port_key, default_port = port_spec
        raw_port = os.getenv(_PLATFORM_PORT_ENV.get(platform_name, "")) or effective.get(
            port_key,
            default_port,
        )
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = default_port
        if 1 <= port <= 65535:
            ports.add(port)
    return tuple(sorted(ports))


def _verification_ports_for_scope(
    scope: str,
    multiplex_config: Any | None,
) -> tuple[int, ...]:
    normalized = normalize_scope(scope)
    if normalized == "webui":
        return VERIFY_PORTS[normalized]
    if multiplex_config is None:
        return VERIFY_PORTS.get(normalized, ())
    return tuple(
        dict.fromkeys(
            (*_multiplex_listener_ports(multiplex_config), *_MULTIPLEX_SURFACE_PORTS[normalized])
        )
    )


def verification_ports_for_scope(scope: str) -> tuple[int, ...]:
    return _verification_ports_for_scope(scope, _multiplex_gateway_config())


def system_restart_targets() -> tuple[RestartTarget, ...]:
    """Return unique system LaunchDaemons the restart helper may sudo-kick."""

    return tuple(target for target in _unique_targets(FULL_HERMES_TARGETS) if target.domain_template == "system")


def _describe_plan(
    scope: str,
    *,
    uid: int,
    multiplex_config: Any | None,
) -> str:
    normalized = normalize_scope(scope)
    if normalized == "webui":
        command = "/restart-webui"
        includes = "Hermes WebUI only"
    elif normalized == "gateways":
        command = "/restart-gateways"
        includes = (
            "single root multiplex gateway, WebUI/dashboard"
            if multiplex_config is not None
            else "default + GPT profile gateways, optional profile gateways, WebUI/dashboard"
        )
    else:
        command = "/restart-hermes"
        includes = "gateway restart scope plus Workspace, watchdog, Codex supervisor, proxies, voice bridge, and Claude-Hermes surfaces"
    lines = [
        f"Restart scope: {normalized}",
        f"Canonical command: {command}",
        f"Includes: {includes}",
        "Launchd targets (configured candidates; user/gui twins resolved at execution):",
    ]
    if multiplex_config is not None:
        lines.append(
            "Bootstrap policy: load configured root/WebUI/dashboard candidates; "
            "never auto-start named profile gateways"
        )
    for target in _targets_for_scope(normalized, multiplex_config):
        req = "required" if target.required else "best-effort"
        desc = f" — {target.description}" if target.description else ""
        lines.append(f"- {target.service_name(uid)} ({req}){desc}")
    ports = _verification_ports_for_scope(normalized, multiplex_config)
    if ports:
        lines.append("Verification ports: " + ", ".join(str(p) for p in ports))
    lines.append(f"Log: {LOG_PATH}")
    return "\n".join(lines)


def describe_plan(scope: str, *, uid: int | None = None) -> str:
    """Return a human-readable dry-run plan without changing runtime state."""

    return _describe_plan(
        scope,
        uid=uid if uid is not None else os.getuid(),
        multiplex_config=_multiplex_gateway_config(),
    )


def _default_sudoers_user() -> str:
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name


def system_restart_sudoers_content(username: str | None = None) -> str:
    """Return the narrow sudoers drop-in for unattended system-service restarts."""

    user = username or _default_sudoers_user()
    if not user or any(ch.isspace() or ch in {":", ",", "=", "\\"} for ch in user):
        raise RestartError(f"Unsafe sudoers username: {user!r}")
    commands = [
        f"{LAUNCHCTL_BIN} kickstart -k {target.service_name(os.getuid())}"
        for target in system_restart_targets()
    ]
    commands.extend(
        f"{LAUNCHCTL_BIN} bootstrap system "
        f"{SYSTEM_LAUNCH_DAEMONS_DIR / f'{target.label}.plist'}"
        for target in system_restart_targets()
        if target.bootstrap_policy is BootstrapPolicy.MULTIPLEX_CONFIGURED
    )
    return (
        "# Managed by Hermes Agent. Allows unattended restarts only for the\n"
        "# Hermes-owned system LaunchDaemons used by /restart-gateways and /restart-hermes.\n"
        f"Cmnd_Alias HERMES_RESTART_SURFACES = {', '.join(commands)}\n"
        f"{user} ALL=(root) NOPASSWD: HERMES_RESTART_SURFACES\n"
    )


def install_system_restart_sudoers(username: str | None = None, *, dry_run: bool = False) -> str:
    """Install the sudoers drop-in that lets detached restarts touch system daemons."""

    content = system_restart_sudoers_content(username)
    if dry_run:
        return content
    tmp_dir = Path(tempfile.mkdtemp(prefix="hermes-sudoers-"))
    tmp_path = tmp_dir / SUDOERS_DROPIN.name
    tmp_path.write_text(content, encoding="utf-8")
    check = _run([VISUDO_BIN, "-cf", str(tmp_path)], timeout=15)
    if check.returncode != 0:
        raise RestartError(f"sudoers validation failed for {tmp_path}: {check.stderr}")
    install = _run(
        [SUDO_BIN, "-n", "install", "-o", "root", "-g", "wheel", "-m", "0440", str(tmp_path), str(SUDOERS_DROPIN)],
        timeout=30,
    )
    if install.returncode != 0:
        raise RestartError(
            "sudoers drop-in validated but could not be installed non-interactively. "
            f"Run once from a local shell: sudo install -o root -g wheel -m 0440 {tmp_path} {SUDOERS_DROPIN}"
        )
    verify = _run([SUDO_BIN, "-n", VISUDO_BIN, "-cf", str(SUDOERS_DROPIN)], timeout=15)
    if verify.returncode != 0:
        raise RestartError(f"installed sudoers drop-in failed validation: {verify.stderr}")
    return f"Installed {SUDOERS_DROPIN} for user {username or _default_sudoers_user()}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{_timestamp()}] {message}\n")


_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "passphrase",
    "passcode",
    "token",
    "secret",
    "key",
    "credential",
    "auth",
    "cookie",
    "bearer",
    "signature",
)
_KEY_VALUE_PATTERN = re.compile(
    r"(?P<key>[\"']?[A-Za-z0-9_.-]+[\"']?)\s*(?P<separator>=>|=|:)"
)


def _redact_sensitive_assignments(text: str) -> str:
    redacted: list[str] = []
    for line in text.splitlines():
        for match in _KEY_VALUE_PATTERN.finditer(line):
            key = match.group("key").strip("\"'").lower()
            if any(marker in key for marker in _SENSITIVE_KEY_MARKERS):
                line = f"{line[:match.end()]}[REDACTED]"
                break
        redacted.append(line)
    return "\n".join(redacted)


def _launchctl_output_summary(output: str) -> str:
    state_match = re.search(
        r"^\s*state\s*=\s*([A-Za-z0-9_.-]+)\s*$",
        output,
        re.MULTILINE,
    )
    pid_match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", output, re.MULTILINE)
    state = state_match.group(1) if state_match else "unknown"
    pid = pid_match.group(1) if pid_match else "none"
    return f"state={state} pid={pid}"


def _run(cmd: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    printable = " ".join(shlex.quote(part) for part in cmd)
    _append_log(f"$ {printable}")
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        timeout_msg = f"timed out after {timeout}s"
        stderr = f"{stderr}\n{timeout_msg}".strip()
        proc = subprocess.CompletedProcess(cmd, 124, stdout, stderr)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if cmd[:2] == [LAUNCHCTL_BIN, "print"]:
        loaded = "yes" if proc.returncode == 0 else "no"
        _append_log(f"launchctl: loaded={loaded} {_launchctl_output_summary(stdout)}")
        detail = stderr or (stdout if proc.returncode != 0 else "")
        if detail:
            _append_log(f"launchctl error: {_redact_sensitive_assignments(detail)[:2000]}")
    elif LAUNCHCTL_BIN in cmd:
        if stdout:
            _append_log(f"stdout: {_redact_sensitive_assignments(stdout)[:2000]}")
        if stderr:
            _append_log(f"stderr: {_redact_sensitive_assignments(stderr)[:2000]}")
    else:
        if stdout:
            _append_log(f"stdout: {stdout[:2000]}")
        if stderr:
            _append_log(f"stderr: {stderr[:2000]}")
    _append_log(f"exit={proc.returncode}")
    return proc


def _launchctl_print(service: str) -> subprocess.CompletedProcess[str]:
    return _run([LAUNCHCTL_BIN, "print", service], timeout=15)


def _kickstart(service: str) -> subprocess.CompletedProcess[str]:
    proc = _run([LAUNCHCTL_BIN, "kickstart", "-k", service], timeout=30)
    if proc.returncode == 0 or not service.startswith("system/"):
        return proc
    return _kickstart_with_sudo(service, proc)


def _kickstart_with_sudo(
    service: str,
    original: subprocess.CompletedProcess[str],
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Retry a system launchd kickstart through non-interactive sudo.

    Touch ID / sudo policy prompts can hang even with ``sudo -n`` on some macOS
    setups. _run() converts that into a normal return-code failure so the
    detached helper can still notify the session and write completion markers.
    """

    sudo = _run([SUDO_BIN, "-n", LAUNCHCTL_BIN, "kickstart", "-k", service], timeout=timeout)
    return sudo if sudo.returncode == 0 else original


def _kickstart_optional(target: RestartTarget, service: str) -> subprocess.CompletedProcess[str]:
    """Kickstart a best-effort target, using sudo only for system services.

    Optional system LaunchDaemons are included in the plan for visibility. They
    are root-owned on macOS, so ``launchctl kickstart -k system/...`` usually
    needs sudo even though user/gui LaunchAgents do not. Retry through
    non-interactive ``sudo -n`` when needed, but keep the target best-effort: a
    missing cached sudo credential must not fail the whole Hermes restart.
    """

    proc = _run([LAUNCHCTL_BIN, "kickstart", "-k", service], timeout=30)
    if proc.returncode != 0 and service.startswith("system/"):
        sudo_proc = _kickstart_with_sudo(service, proc)
        if sudo_proc.returncode == 0:
            return sudo_proc
        _append_log(
            f"{service} optional system target not restarted; sudo was required "
            f"but unavailable non-interactively ({target.description or target.label})"
        )
    return proc


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_is_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _gateway_status_path_for_target(target: RestartTarget) -> Path | None:
    return GATEWAY_STATUS_PATHS.get(target.label)


def _webui_busy_details(targets: Iterable[RestartTarget]) -> list[str]:
    """Report active WebUI work, failing closed when health cannot be trusted."""
    if not any(target.label in WEBUI_BUSY_LABELS for target in targets):
        return []
    try:
        import urllib.request

        with urllib.request.urlopen(WEBUI_HEALTH_URL, timeout=WEBUI_HEALTH_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            raise ValueError("health payload is not an object")
        active_runs = payload["active_runs"]
        if isinstance(active_runs, bool) or not isinstance(active_runs, int):
            raise ValueError("active_runs is not an integer")
        if active_runs < 0:
            raise ValueError("active_runs is negative")
    except Exception as exc:
        return [
            "ai.hermes.webui: health probe unavailable or invalid; "
            f"refusing restart ({type(exc).__name__})"
        ]
    if active_runs > 0:
        oldest = payload.get("oldest_run_age_seconds")
        detail = f"ai.hermes.webui: active_runs={active_runs}"
        if oldest is not None:
            detail += f", oldest_run_age_seconds={oldest}"
        return [detail]
    return []


def _desktop_busy_details(targets: Iterable[RestartTarget]) -> list[str]:
    """Report dashboard-backed Desktop turns that a restart would destroy."""
    if not any(target.label in DESKTOP_BUSY_LABELS for target in targets):
        return []
    if not _desktop_dashboard_is_running():
        return []
    try:
        import urllib.request

        with urllib.request.urlopen(DESKTOP_HEALTH_URL, timeout=WEBUI_HEALTH_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            raise ValueError("health payload is not an object")
        if "active_runs" not in payload and payload.get("marker") == DESKTOP_HEALTH_MARKER:
            _append_log(
                "desktop dashboard uses legacy health payload; allowing one-time upgrade restart"
            )
            return []
        active_runs = payload["active_runs"]
        if isinstance(active_runs, bool) or not isinstance(active_runs, int):
            raise ValueError("active_runs is not an integer")
        if active_runs < 0:
            raise ValueError("active_runs is negative")
    except Exception as exc:
        return [
            "ai.hermes.desktop-remote-dashboard: health probe unavailable or invalid; "
            f"refusing restart ({type(exc).__name__})"
        ]
    if active_runs > 0:
        oldest = payload.get("oldest_run_age_seconds")
        detail = f"ai.hermes.desktop-remote-dashboard: active_runs={active_runs}"
        if oldest is not None:
            detail += f", oldest_run_age_seconds={oldest}"
        return [detail]
    return []


def _iso_age_seconds(value: Any) -> float | None:
    """Age in seconds of an RFC3339 timestamp, or ``None`` if unparseable."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _heartbeat_active_agents(target: RestartTarget) -> tuple[int, float] | None:
    """Live ``active_agents`` from the loop heartbeat, when fresh.

    ``gateway_state.json`` only rewrites on turn/claim/release boundaries; the
    loop heartbeat (``gateway.shutdown_watchdog``) is rewritten on a fixed
    ~30s cadence regardless of activity, with a live ``active_agents``
    snapshot merged in every tick (gateway/run.py's ``loop_heartbeat_forever``
    call). That makes it an independent cross-check for exactly the case
    where the state file might be stale: a single long turn past the trust
    window, a persist that silently failed, or a foreign clobber of the
    state file. Returns ``None`` when the heartbeat is missing, older than
    ``HEARTBEAT_FRESH_WINDOW_S``, or predates this field (older gateway
    build) — callers must treat that as "cannot confirm", not "confirmed
    idle".
    """
    path = _heartbeat_path_for_target(target)
    if path is None:
        return None
    payload = _read_json(path)
    if not payload:
        return None
    age = _iso_age_seconds(payload.get("updated_at"))
    if age is None or not (-HEARTBEAT_FRESH_WINDOW_S <= age <= HEARTBEAT_FRESH_WINDOW_S):
        return None
    if "active_agents" not in payload:
        return None
    try:
        return int(payload["active_agents"]), age
    except (TypeError, ValueError):
        return None


def gateway_busy_snapshot(targets: Iterable[RestartTarget]) -> list[dict[str, Any]]:
    """Structured per-target busy-state record: label, active_agents,
    gateway_state, restart_requested, and the freshness age behind that
    active_agents count.

    This is the single source of the trust-window/heartbeat cross-check
    logic; ``_gateway_busy_details`` (the plain-text explanation used by the
    safe-restart wait) and the update service's drain-audit evidence both
    read off this same computation instead of re-deriving it.
    """
    snapshot: list[dict[str, Any]] = []
    for target in targets:
        status_path = _gateway_status_path_for_target(target)
        if status_path is None:
            continue
        payload = _read_json(status_path)
        if not payload:
            continue
        if not _pid_is_alive(payload.get("pid")):
            continue
        try:
            active_agents = int(payload.get("active_agents") or 0)
        except (TypeError, ValueError):
            active_agents = 0
        gateway_state = str(payload.get("gateway_state") or "unknown")
        restart_requested = bool(payload.get("restart_requested"))
        active_agents_age: float | None = None
        heartbeat_active_agents: int | None = None
        heartbeat_age: float | None = None

        # Freshness comes ONLY from the count's dedicated stamp. A missing
        # active_agents_updated_at means UNKNOWN freshness (an old gateway that
        # predates the stamp, or a failed recount that skipped it) — never fall
        # back to updated_at, which the watchdog identity-restamps every ~30s
        # and would re-bless a stale count forever (Codex batch-2 review P1-2).
        # Verify BOTH zero and nonzero counts: a stale/unknown ZERO must not
        # authorize a restart during a live run either (Codex batch-2 P1-3).
        _stamp = payload.get("active_agents_updated_at")
        active_agents_age = _iso_age_seconds(_stamp) if _stamp else None
        _count_is_fresh = (
            active_agents_age is not None
            and active_agents_age <= ACTIVE_AGENTS_TRUST_WINDOW_S
        )
        if not _count_is_fresh:
            live = _heartbeat_active_agents(target)
            if live is None:
                # No independent confirmation: fail closed for NONZERO counts.
                # For ZERO there is one sanctioned exception: a LEGACY writer
                # (no stamp field at all — a gateway predating the stamp) can
                # never prove freshness, so demanding it deadlocks every
                # first-upgrade restart (observed live 2026-07-24: the old
                # gateway idled for 6h while the new helper refused its zero).
                # A legacy zero with fresh updated_at and a live pid is the
                # best evidence a legacy writer can produce and matches
                # pre-stamp behavior; once the NEW gateway runs, the stamp
                # exists and the strict path applies. A PRESENT-but-stale
                # stamp still fails closed — that writer could have stamped
                # and didn't.
                if active_agents == 0:
                    _legacy_writer = "active_agents_updated_at" not in payload
                    _updated_age = _iso_age_seconds(payload.get("updated_at"))
                    if (
                        _legacy_writer
                        and _updated_age is not None
                        and _updated_age <= ACTIVE_AGENTS_TRUST_WINDOW_S
                    ):
                        _append_log(
                            f"{target.label}: active_agents=0 from a legacy "
                            f"writer (no count stamp), updated_at fresh "
                            f"({_updated_age:.1f}s) and pid live; accepting "
                            "legacy idle (first-upgrade transition)"
                        )
                    else:
                        _append_log(
                            f"{target.label}: active_agents=0 but count freshness "
                            f"is unknown (stamp={_stamp!r}, age={active_agents_age!r}s) "
                            "and no heartbeat is available; treating as BUSY "
                            "(failing closed)"
                        )
                        active_agents = 1  # force busy; unverifiable idle is not idle
                else:
                    _append_log(
                        f"{target.label}: active_agents={active_agents} but status "
                        f"file freshness is unknown/stale (age={active_agents_age!r}s, "
                        f"trust_window={ACTIVE_AGENTS_TRUST_WINDOW_S}s) and no fresh "
                        "heartbeat is available; trusting the file (failing closed)"
                    )
            else:
                heartbeat_active_agents, heartbeat_age = live
                if heartbeat_active_agents != active_agents:
                    _append_log(
                        f"{target.label}: status file active_agents="
                        f"{active_agents} freshness unknown/stale "
                        f"(age={active_agents_age!r}s); fresh heartbeat reports "
                        f"active_agents={heartbeat_active_agents} "
                        f"(age={heartbeat_age:.1f}s) — trusting the heartbeat"
                    )
                active_agents = heartbeat_active_agents

        snapshot.append(
            {
                "label": target.label,
                "status_path": str(status_path),
                "active_agents": active_agents,
                "gateway_state": gateway_state,
                "restart_requested": restart_requested,
                "active_agents_age_seconds": active_agents_age,
                "heartbeat_active_agents": heartbeat_active_agents,
                "heartbeat_age_seconds": heartbeat_age,
                "busy": bool(
                    active_agents > 0
                    or (restart_requested and gateway_state == "draining")
                ),
            }
        )
    return snapshot


def _gateway_busy_details(targets: Iterable[RestartTarget]) -> list[str]:
    return [
        f"{record['label']}: active_agents={record['active_agents']}, "
        f"state={record['gateway_state']}, "
        f"restart_requested={record['restart_requested']}, "
        f"status={record['status_path']}"
        for record in gateway_busy_snapshot(targets)
        if record["busy"]
    ]


def busy_state_snapshot(scope: str = "hermes") -> dict[str, Any]:
    """Combined structured + plain-text busy-state evidence for a scope.

    Reuses the same helpers ``_wait_for_safe_restart`` polls during a real
    restart, so this is exactly what a caller would see if it asked "why is
    the drain stuck" at that instant — safe to embed verbatim into another
    process's evidence trail (e.g. the update service's drain-audit.jsonl).
    """
    targets = targets_for_scope(scope)
    return {
        "scope": normalize_scope(scope),
        "gateway": gateway_busy_snapshot(targets),
        "webui": _webui_busy_details(targets),
        "desktop": _desktop_busy_details(targets),
    }



def _wait_for_safe_restart(
    targets: Iterable[RestartTarget],
    *,
    timeout: float = DEFAULT_SAFE_WAIT_TIMEOUT,
    interval: float = DEFAULT_SAFE_WAIT_INTERVAL,
) -> tuple[bool, list[str]]:
    """Poll gateway runtime status until no target reports active work."""
    deadline = time.monotonic() + max(0.0, timeout)
    last_busy: list[str] = []
    while True:
        last_busy = (
            _gateway_busy_details(targets)
            + _webui_busy_details(targets)
            + _desktop_busy_details(targets)
        )
        if not last_busy:
            _append_log("safe restart check passed")
            return True, []
        _append_log("safe restart waiting: " + " | ".join(last_busy))
        if time.monotonic() >= deadline:
            return False, last_busy
        time.sleep(max(0.1, interval))


def _wait_for_webui_safe_restart(
    target: RestartTarget,
    *,
    timeout: float = DEFAULT_SAFE_WAIT_TIMEOUT,
    interval: float = DEFAULT_SAFE_WAIT_INTERVAL,
) -> tuple[bool, list[str]]:
    """Re-check only WebUI work at its immediate restart boundary."""
    deadline = time.monotonic() + max(0.0, timeout)
    last_busy: list[str] = []
    while True:
        last_busy = _webui_busy_details((target,))
        if not last_busy:
            _append_log("final WebUI safe restart check passed")
            return True, []
        _append_log("final WebUI safe restart waiting: " + " | ".join(last_busy))
        if time.monotonic() >= deadline:
            return False, last_busy
        time.sleep(max(0.1, interval))


def _wait_for_desktop_safe_restart(
    target: RestartTarget,
    *,
    timeout: float = DEFAULT_SAFE_WAIT_TIMEOUT,
    interval: float = DEFAULT_SAFE_WAIT_INTERVAL,
) -> tuple[bool, list[str]]:
    """Re-check Desktop turns at the dashboard restart boundary."""
    deadline = time.monotonic() + max(0.0, timeout)
    last_busy: list[str] = []
    while True:
        last_busy = _desktop_busy_details((target,))
        if not last_busy:
            _append_log("final Desktop safe restart check passed")
            return True, []
        _append_log("final Desktop safe restart waiting: " + " | ".join(last_busy))
        if time.monotonic() >= deadline:
            return False, last_busy
        time.sleep(max(0.1, interval))



def _completion_message(scope: str, exit_code: int) -> str:
    normalized = normalize_scope(scope)
    if normalized == "webui":
        label = "Hermes WebUI"
    elif normalized == "gateways":
        label = "Hermes gateways"
    else:
        label = "Hermes surfaces"
    if exit_code == 0:
        return f"Done — {label} restart finished."
    return f"{label} restart finished with errors. Check {LOG_PATH}"


def _notify_origin(origin_json: str | None, message: str) -> None:
    if not origin_json:
        return
    try:
        origin = json.loads(origin_json)
        platform_name = str(origin.get("platform") or "").lower()
        chat_id = str(origin.get("chat_id") or "")
        thread_id = origin.get("thread_id")
        if not platform_name or not chat_id:
            raise RestartError("notification origin missing platform/chat_id")

        from gateway.config import Platform, load_gateway_config
        from tools.send_message_tool import _send_to_platform

        platform = Platform(platform_name)
        config = load_gateway_config()
        pconfig = config.platforms.get(platform)
        if not pconfig or not pconfig.enabled:
            raise RestartError(f"notification platform {platform_name!r} is not enabled")
        result = asyncio.run(_send_to_platform(platform, pconfig, chat_id, message, thread_id=thread_id))
        if isinstance(result, dict) and result.get("error"):
            raise RestartError(str(result["error"]))
        suffix = f":{thread_id}" if thread_id else ""
        _append_log(f"completion notification sent to {platform_name}:{chat_id}{suffix}")
    except Exception as exc:
        _append_log(f"completion notification failed: {exc}")


def _notify_tty(tty_path: str | None, message: str) -> None:
    if not tty_path:
        return
    try:
        with open(tty_path, "a", encoding="utf-8", buffering=1) as tty:
            tty.write(f"\n{message}\n")
        _append_log(f"completion notification wrote to tty {tty_path}")
    except Exception as exc:
        _append_log(f"tty completion notification failed: {exc}")


def _write_completion_marker(marker_path: str | None, scope: str, exit_code: int, message: str) -> None:
    if not marker_path:
        return
    try:
        path = Path(marker_path).expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "status": "complete",
            "scope": normalize_scope(scope),
            "exit_code": int(exit_code),
            "message": message,
            "completed_at": _timestamp(),
            "log_path": str(LOG_PATH),
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(json.dumps(payload, separators=(",", ":")))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        _append_log(f"completion marker wrote to {path}")
    except Exception as exc:
        _append_log(f"completion marker failed: {exc}")


def _alternate_launchd_service(service: str) -> str | None:
    """Return the user/gui twin for profile LaunchAgents, if applicable."""
    if service.startswith("user/"):
        rest = service[len("user/"):]
        uid, _, label = rest.partition("/")
        if uid and label:
            return f"gui/{uid}/{label}"
    if service.startswith("gui/"):
        rest = service[len("gui/"):]
        uid, _, label = rest.partition("/")
        if uid and label:
            return f"user/{uid}/{label}"
    return None


def _resolve_loaded_service(service: str) -> tuple[str, subprocess.CompletedProcess[str]]:
    """Resolve a launchd service, trying the user/gui twin when needed."""
    result = _launchctl_print(service)
    if result.returncode == 0:
        return service, result
    alternate = _alternate_launchd_service(service)
    if alternate:
        alt_result = _launchctl_print(alternate)
        if alt_result.returncode == 0:
            _append_log(f"{service} not loaded; using loaded alternate {alternate}")
            return alternate, alt_result
    return service, result


def _target_plist_path(target: RestartTarget) -> Path | None:
    if target.domain_template == "system":
        return SYSTEM_LAUNCH_DAEMONS_DIR / f"{target.label}.plist"
    if target.domain_template.startswith(("user/", "gui/")):
        return USER_LAUNCH_AGENTS_DIR / f"{target.label}.plist"
    return None


def _bootstrap_domains(target: RestartTarget, uid: int, plist_path: Path) -> tuple[str, ...]:
    requested = target.domain(uid)
    if requested == "system":
        return (requested,)
    alternate_service = _alternate_launchd_service(f"{requested}/{target.label}")
    alternate = alternate_service.rsplit("/", 1)[0] if alternate_service else None
    domains = list(dict.fromkeys(domain for domain in (requested, alternate) if domain))
    try:
        payload = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        payload = {}
    raw_session_types = payload.get("LimitLoadToSessionType") if isinstance(payload, dict) else None
    if isinstance(raw_session_types, str):
        session_types = {raw_session_types}
    elif isinstance(raw_session_types, (list, tuple)):
        session_types = {str(value) for value in raw_session_types}
    else:
        session_types = set()
    gui_domain = f"gui/{uid}"
    if "Aqua" in session_types and gui_domain in domains:
        domains.remove(gui_domain)
        domains.insert(0, gui_domain)
    return tuple(domains)


def _bootstrap_service(domain: str, plist_path: Path) -> subprocess.CompletedProcess[str]:
    cmd = [LAUNCHCTL_BIN, "bootstrap", domain, str(plist_path)]
    proc = _run(cmd, timeout=30)
    if proc.returncode == 0 or domain != "system":
        return proc
    sudo = _run([SUDO_BIN, "-n", *cmd], timeout=30)
    return sudo if sudo.returncode == 0 else proc


def _wait_for_target_loaded(
    requested_service: str,
    *,
    timeout: float = DEFAULT_BOOTSTRAP_WAIT_TIMEOUT,
    interval: float = DEFAULT_BOOTSTRAP_WAIT_INTERVAL,
) -> tuple[str, subprocess.CompletedProcess[str]]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        service, result = _resolve_loaded_service(requested_service)
        if result.returncode == 0 or time.monotonic() >= deadline:
            return service, result
        time.sleep(max(0.05, interval))


def _bootstrap_targets_before_drain(
    targets: Iterable[RestartTarget],
    *,
    uid: int,
    enabled: bool,
) -> list[str]:
    if not enabled:
        return []
    failures: list[str] = []
    for target in targets:
        if target.bootstrap_policy is not BootstrapPolicy.MULTIPLEX_CONFIGURED:
            continue
        requested_service = target.service_name(uid)
        service, result = _resolve_loaded_service(requested_service)
        if result.returncode == 0:
            continue
        plist_path = _target_plist_path(target)
        if plist_path is None or not plist_path.is_file():
            message = f"{requested_service} is not loaded and has no configured launchd plist"
            _append_log(message)
            if target.required:
                failures.append(message)
            continue
        for domain in _bootstrap_domains(target, uid, plist_path):
            bootstrap = _bootstrap_service(domain, plist_path)
            if bootstrap.returncode != 0:
                service, result = _resolve_loaded_service(requested_service)
                if result.returncode == 0:
                    _append_log(f"configured target became loaded as {service}")
                    break
                continue
            service, result = _wait_for_target_loaded(requested_service)
            if result.returncode == 0:
                _append_log(f"bootstrapped configured target {service} from {plist_path}")
                break
        if result.returncode == 0:
            continue
        message = f"{requested_service} could not bootstrap from {plist_path}"
        _append_log(message)
        if target.required:
            failures.append(message)
    return failures


def _launchctl_pid(result: subprocess.CompletedProcess[str]) -> int | None:
    """Extract a positive PID from ``launchctl print`` output."""
    match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout or "", re.MULTILINE)
    if not match:
        return None
    pid = int(match.group(1))
    return pid if pid > 0 else None


def _heartbeat_path_for_target(target: RestartTarget) -> Path | None:
    """Return the loop-heartbeat path for a gateway target's HERMES_HOME."""
    status_path = _gateway_status_path_for_target(target)
    if status_path is None:
        return None
    return status_path.parent.joinpath(*_HEARTBEAT_RELATIVE)


def _heartbeat_confirms_pid(target: RestartTarget, launchd_pid: int) -> bool:
    """Return True when a fresh loop heartbeat vouches for the launchd PID.

    ``gateway_state.json`` only rewrites on turns/transitions and can be
    clobbered by a foreign writer; the heartbeat is rewritten every ~30s by the
    gateway's own event loop, so pid agreement plus a fresh ``updated_at`` is
    strong evidence the launchd PID really is the live gateway.
    """
    path = _heartbeat_path_for_target(target)
    if path is None:
        return False
    payload = _read_json(path)
    if not payload:
        return False
    try:
        heartbeat_pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        return False
    if heartbeat_pid != launchd_pid:
        return False
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, str):
        return False
    raw = updated_at.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    # A future-dated heartbeat (clock rollback, forged timestamp) must not be
    # trusted indefinitely; allow only small skew.
    return -HEARTBEAT_FRESH_WINDOW_S <= age <= HEARTBEAT_FRESH_WINDOW_S


def _port_listener_pids(port: int) -> set[int]:
    """Return PIDs holding a LISTEN socket on ``port`` (empty on failure)."""
    result = _run(["bash", "-lc", f"lsof -nP -iTCP:{port} -sTCP:LISTEN -t"], timeout=10)
    pids: set[int] = set()
    for token in (result.stdout or "").split():
        try:
            pids.add(int(token))
        except ValueError:
            continue
    return pids


def _gateway_pid(target: RestartTarget, launchctl_result: subprocess.CompletedProcess[str]) -> int | None:
    launchd_pid = _launchctl_pid(launchctl_result)
    if launchd_pid is None:
        return None
    status_path = _gateway_status_path_for_target(target)
    if status_path is not None:
        payload = _read_json(status_path) or {}
        from gateway.status import get_runtime_status_running_pid

        runtime_pid = get_runtime_status_running_pid(
            payload,
            expected_home=status_path.parent,
        )
        if runtime_pid == launchd_pid:
            return launchd_pid
        # gateway_state.json disagrees with launchd (stale, dead PID, or
        # clobbered by a foreign writer). It only rewrites on turns/transitions,
        # so it never self-heals; refusing here on the status file alone would
        # block graceful restarts forever. Fall back to independent evidence
        # that launchd's PID really is the live gateway.
        if _heartbeat_confirms_pid(target, launchd_pid):
            _append_log(
                f"{target.label}: status file {status_path} pid="
                f"{payload.get('pid')!r} does not match launchd pid {launchd_pid}; "
                "trusting launchd via fresh loop heartbeat (stale/foreign state file)"
            )
            return launchd_pid
        if _pid_is_alive(launchd_pid) and launchd_pid in _port_listener_pids(GATEWAY_LISTENER_PORT):
            _append_log(
                f"{target.label}: status file {status_path} pid="
                f"{payload.get('pid')!r} does not match launchd pid {launchd_pid} "
                f"and no fresh heartbeat; trusting launchd because pid {launchd_pid} "
                f"is alive and owns listener port {GATEWAY_LISTENER_PORT}"
            )
            return launchd_pid
    return None


def _graceful_restart_gateway(
    target: RestartTarget,
    service: str,
    launchctl_result: subprocess.CompletedProcess[str],
    *,
    timeout: float,
) -> tuple[RestartVerification, str | None]:
    """Ask one gateway to drain and self-restart; never hard-kill it."""
    pid = _gateway_pid(target, launchctl_result)
    if pid is None:
        return RestartVerification.NOT_RESTARTED, f"{service} has no verifiable gateway PID; refusing hard restart"
    if not hasattr(signal, "SIGUSR1"):
        return RestartVerification.NOT_RESTARTED, f"{service} cannot restart gracefully on this platform"
    try:
        os.kill(pid, signal.SIGUSR1)
    except (OSError, PermissionError, ProcessLookupError) as exc:
        return RestartVerification.NOT_RESTARTED, f"{service} graceful restart signal failed: {exc}"

    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        # launchd owns the service identity and is the authoritative proof that
        # this specific label restarted. A status file can briefly advertise a
        # different live PID while launchd still owns the original process; if
        # that stale/transient value is treated as success, the outer dedup set
        # incorrectly suppresses the user/gui twin without restarting either.
        current = _launchctl_print(service)
        replacement_pid = _launchctl_pid(current) if current.returncode == 0 else None
        if replacement_pid is not None:
            if replacement_pid != pid:
                # A launchd PID change proves the supervisor started a replacement,
                # but it does not prove the old process released its listeners or
                # that gateway_state.json belongs to the replacement yet. Waiting
                # for both prevents the old/new overlap that produced transient
                # token and port-binding conflicts during chained restarts.
                if _pid_is_alive(pid):
                    time.sleep(0.25)
                    continue
                if _gateway_pid(target, current) == replacement_pid:
                    return RestartVerification.RESTARTED, None
        else:
            # A failed/empty launchctl read is not proof that the old PID
            # survived. Retry briefly; if launchd remains unreadable, dedupe the
            # user/gui twin rather than risking a second signal to fresh work.
            for _attempt in range(2):
                time.sleep(0.1)
                retry = _launchctl_print(service)
                retry_pid = _launchctl_pid(retry) if retry.returncode == 0 else None
                if retry_pid is None:
                    continue
                if retry_pid != pid:
                    if _pid_is_alive(pid):
                        continue
                    if _gateway_pid(target, retry) == retry_pid:
                        return RestartVerification.RESTARTED, None
                break
            else:
                return (
                    RestartVerification.UNVERIFIABLE,
                    f"{service} restart verification was inconclusive after transient "
                    "launchctl read failures",
                )
        time.sleep(0.25)
    return (
        RestartVerification.NOT_RESTARTED,
        f"{service} did not complete its graceful self-restart within {timeout:g}s",
    )


def _verify_listen_port(port: int) -> str | None:
    result = _run(["bash", "-lc", f"lsof -nP -iTCP:{port} -sTCP:LISTEN >/dev/null"], timeout=10)
    if result.returncode != 0:
        return f"port {port} is not listening"
    return None


def _verify_http_url(url: str, expected: tuple[int, ...] = (200, 401)) -> str | None:
    quoted = shlex.quote(url)
    tests = " || ".join(f'[ "$code" = "{int(code)}" ]' for code in expected)
    script = (
        f"code=$(curl -sk --max-time 5 -o /dev/null -w '%{{http_code}}' {quoted} || true); "
        f"if {tests}; then exit 0; fi; echo $code; exit 1"
    )
    result = _run(["bash", "-lc", script], timeout=8)
    if result.returncode != 0:
        observed = (result.stdout or result.stderr or "?").strip()
        return f"HTTP probe {url} returned {observed or 'non-2xx'}"
    return None


def _verify_scope_health(
    scope: str,
    *,
    active_labels: set[str] | None = None,
    verify_ports: Iterable[int] | None = None,
) -> list[str]:
    """Backward-compatible wrapper: return REQUIRED failures only.

    Best-effort probes (dashboard ports) are checked separately via
    :func:`_verify_scope_health_split` and must not drive the exit code.
    """
    required, _best_effort = _verify_scope_health_split(
        scope,
        active_labels=active_labels,
        verify_ports=verify_ports,
    )
    return required


def _verify_scope_health_split(
    scope: str,
    *,
    active_labels: set[str] | None = None,
    verify_ports: Iterable[int] | None = None,
) -> tuple[list[str], list[str]]:
    """Probe scope health, partitioning failures into (required, best_effort).

    Only required failures should fail the restart's exit code; best-effort
    ones (a slow/throttled dashboard daemon) are logged as warnings so an
    otherwise-healthy restart is not falsely reported as a failure.
    """
    required: list[str] = []
    best_effort: list[str] = []
    ports = list(
        verification_ports_for_scope(scope)
        if verify_ports is None
        else verify_ports
    )
    # The MacBook remote dashboard is optional. Its absent port must not fail an
    # otherwise healthy restart when that LaunchAgent is not loaded.
    if active_labels is not None and "ai.hermes.desktop-remote-dashboard" not in active_labels:
        ports = [port for port in ports if port != 9120]
    if not ports:
        return required, best_effort

    def _bucket_for(port: int) -> list[str]:
        return best_effort if port in BEST_EFFORT_VERIFY_PORTS else required

    for port in ports:
        failure = _verify_listen_port(port)
        if failure:
            _bucket_for(port).append(failure)
    # HTTP probes, tagged with the port they exercise so each lands in the
    # correct bucket (dashboard 9119/9120 are best-effort; 8787 is required).
    probes: list[tuple[int, str]] = []
    if scope in {"webui", "gateways", "hermes"}:
        probes.append((8787, "http://127.0.0.1:8787/health"))
    if 9119 in ports:
        probes.append((9119, "http://127.0.0.1:9119/"))
    if 9120 in ports:
        probes.append((9120, "http://127.0.0.1:9120/"))
    if 9119 in ports:
        probes.append((9119, "https://macstudio.tailf7342a.ts.net:9119/"))
    for port, url in probes:
        failure = _verify_http_url(url)
        if failure:
            _bucket_for(port).append(failure)
    return required, best_effort


def _wait_for_scope_health(
    scope: str,
    *,
    active_labels: set[str],
    verify_ports: Iterable[int] | None = None,
    timeout: float | None = None,
    interval: float | None = None,
) -> tuple[list[str], list[str]]:
    """Poll boundedly so normal launchd startup latency is not a false failure.

    Returns (required_failures, best_effort_failures). Only required failures
    should drive the restart exit code. Keeps polling while REQUIRED failures
    remain; a lingering best-effort dashboard lag does not extend the wait past
    the point where every required surface is healthy.

    ``timeout``/``interval`` default to the module-level constants resolved at
    call time (not bound at def time) so tests can shrink the window via
    monkeypatch without spinning to the real deadline.
    """
    if timeout is None:
        timeout = DEFAULT_HEALTH_WAIT_TIMEOUT
    if interval is None:
        interval = DEFAULT_HEALTH_WAIT_INTERVAL
    deadline = time.monotonic() + max(0.0, timeout)
    required: list[str] = []
    best_effort: list[str] = []
    while True:
        required, best_effort = _verify_scope_health_split(
            scope,
            active_labels=active_labels,
            verify_ports=verify_ports,
        )
        if not required or time.monotonic() >= deadline:
            return required, best_effort
        time.sleep(max(0.1, interval))


def restart_scope(
    scope: str,
    *,
    delay: float = 1.0,
    dry_run: bool = False,
    notify_origin_json: str | None = None,
    notify_tty: str | None = None,
    completion_marker: str | None = None,
    safe_wait_timeout: float = DEFAULT_SAFE_WAIT_TIMEOUT,
    safe_wait_interval: float = DEFAULT_SAFE_WAIT_INTERVAL,
) -> int:
    """Restart a configured Hermes surface scope.

    Returns a process-style exit code. Dry-run performs discovery/logging only.
    """

    normalized = normalize_scope(scope)
    uid = os.getuid()
    multiplex_config = _multiplex_gateway_config()
    verify_ports = _verification_ports_for_scope(normalized, multiplex_config)
    _append_log(f"restart requested scope={normalized} dry_run={dry_run} uid={uid}")
    _append_log(
        _describe_plan(
            normalized,
            uid=uid,
            multiplex_config=multiplex_config,
        ).replace("\n", " | ")
    )
    if dry_run:
        return 0
    if delay > 0:
        time.sleep(delay)

    failures: list[str] = []
    targets = (
        _targets_for_scope(normalized, multiplex_config)
        if multiplex_config is not None
        else targets_for_scope(normalized)
    )
    failures.extend(
        _bootstrap_targets_before_drain(
            targets,
            uid=uid,
            enabled=multiplex_config is not None,
        )
    )
    if failures:
        _append_log("restart bootstrap failed: " + "; ".join(failures))
        message = _completion_message(normalized, 1)
        _notify_origin(notify_origin_json, message)
        _notify_tty(notify_tty, message)
        _write_completion_marker(completion_marker, normalized, 1, message)
        return 1
    safe, busy = _wait_for_safe_restart(
        targets,
        timeout=safe_wait_timeout,
        interval=safe_wait_interval,
    )
    if not safe:
        failures.append("safe restart wait timed out: " + " | ".join(busy))
        _append_log(failures[-1])
        message = _completion_message(normalized, 1)
        _notify_origin(notify_origin_json, message)
        _notify_tty(notify_tty, message)
        _write_completion_marker(completion_marker, normalized, 1, message)
        return 1

    restarted_services: set[str] = set()
    active_labels: set[str] = set()
    for target in targets:
        # The scope-level drain above can be followed by many slow launchd
        # operations before the WebUI target is reached. Re-check at the
        # destructive boundary so a chat run that started in that gap is not
        # killed by this restart.
        if target.label in WEBUI_BUSY_LABELS:
            safe, busy = _wait_for_webui_safe_restart(
                target,
                timeout=safe_wait_timeout,
                interval=safe_wait_interval,
            )
            if not safe:
                msg = "final WebUI safe restart wait timed out: " + " | ".join(busy)
                failures.append(msg)
                _append_log(msg)
                break
        if target.label in DESKTOP_BUSY_LABELS:
            safe, busy = _wait_for_desktop_safe_restart(
                target,
                timeout=safe_wait_timeout,
                interval=safe_wait_interval,
            )
            if not safe:
                msg = "final Desktop safe restart wait timed out: " + " | ".join(busy)
                failures.append(msg)
                _append_log(msg)
                break
        requested_service = target.service_name(uid)
        service, before = _resolve_loaded_service(requested_service)
        if before.returncode != 0:
            msg = f"{requested_service} is not loaded"
            _append_log(msg)
            if target.required:
                failures.append(msg)
            continue
        active_labels.add(target.label)
        if service in restarted_services:
            _append_log(
                f"{requested_service} resolves to already restarted {service}; "
                "skipping duplicate"
            )
            continue
        if _gateway_status_path_for_target(target) is not None:
            gateway_timeout = min(max(safe_wait_timeout, 30.0), 600.0)
            verification, detail = _graceful_restart_gateway(
                target,
                service,
                before,
                timeout=gateway_timeout,
            )
            if verification is RestartVerification.NOT_RESTARTED:
                failure = detail or f"{service} restart was not verified"
                failures.append(failure)
                _append_log(failure)
                continue
            restarted_services.add(service)
            if verification is RestartVerification.UNVERIFIABLE:
                _append_log(f"restart verification warning: {detail}")
            else:
                _append_log(f"{service} completed graceful self-restart")
            continue
        kicked = _kickstart(service) if target.required else _kickstart_optional(target, service)
        if kicked.returncode != 0:
            msg = f"{service} restart failed"
            _append_log(msg)
            if target.required:
                failures.append(msg)
            continue
        restarted_services.add(service)
        time.sleep(0.4)
        after = _launchctl_print(service)
        if after.returncode != 0:
            msg = f"{service} did not verify after restart"
            _append_log(msg)
            if target.required:
                failures.append(msg)

    required_failures, best_effort_failures = _wait_for_scope_health(
        normalized,
        active_labels=active_labels,
        verify_ports=verify_ports,
    )
    for failure in best_effort_failures:
        _append_log(f"restart verification warning (best-effort): {failure}")
    for failure in required_failures:
        _append_log(f"restart verification failed: {failure}")
    failures.extend(required_failures)

    if failures:
        _append_log("restart completed with required failures: " + "; ".join(failures))
        message = _completion_message(normalized, 1)
        _notify_origin(notify_origin_json, message)
        _notify_tty(notify_tty, message)
        _write_completion_marker(completion_marker, normalized, 1, message)
        return 1
    _append_log("restart completed")
    message = _completion_message(normalized, 0)
    _notify_origin(notify_origin_json, message)
    _notify_tty(notify_tty, message)
    _write_completion_marker(completion_marker, normalized, 0, message)
    return 0


def enqueue_detached_restart(
    scope: str,
    *,
    delay: float = 1.0,
    dry_run: bool = False,
    notify_origin: dict[str, Any] | None = None,
    notify_tty: str | None = None,
    completion_marker: str | None = None,
    safe_wait_timeout: float | None = None,
    safe_wait_interval: float | None = None,
) -> str:
    """Spawn an OS-session-independent restart worker and return immediately.

    This is the only supported entry point from a gateway, WebUI, Desktop,
    slash worker, or agent-owned process. The child gets a new session, null
    stdin, independent log descriptors, and no inherited descriptors, so it
    survives termination of the caller while restarting that caller's service.
    """

    normalized = normalize_scope(scope)
    if dry_run:
        return describe_plan(normalized)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.restart_surfaces",
        "--scope",
        normalized,
        "--delay",
        str(delay),
        "--detached-worker",
    ]
    if notify_origin:
        cmd.extend(["--notify-origin-json", json.dumps(notify_origin, separators=(",", ":"))])
    if notify_tty:
        cmd.extend(["--notify-tty", notify_tty])
    if completion_marker:
        cmd.extend(["--completion-marker", completion_marker])
    if safe_wait_timeout is not None:
        cmd.extend(["--safe-wait-timeout", str(safe_wait_timeout)])
    if safe_wait_interval is not None:
        cmd.extend(["--safe-wait-interval", str(safe_wait_interval)])
    with LOG_PATH.open("a", encoding="utf-8") as log_fh:
        subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
    notify_note = " I'll send a follow-up here when it finishes." if (notify_origin or notify_tty) else ""
    drain_note = (
        "live WebUI chat turns"
        if normalized == "webui"
        else "active gateway tasks and live WebUI chat turns"
    )
    return (
        f"Queued detached Hermes {normalized} restart. "
        f"It will wait for {drain_note} to finish before restarting."
        f"{notify_note} Log: {LOG_PATH}"
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restart Hermes launchd surfaces from a detached helper")
    parser.add_argument("--scope", default="gateways", choices=("webui", "gateways", "hermes"))
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--notify-origin-json", default=None)
    parser.add_argument("--notify-tty", default=None)
    parser.add_argument("--completion-marker", default=None)
    parser.add_argument("--safe-wait-timeout", type=float, default=DEFAULT_SAFE_WAIT_TIMEOUT)
    parser.add_argument("--safe-wait-interval", type=float, default=DEFAULT_SAFE_WAIT_INTERVAL)
    parser.add_argument(
        "--install-system-restart-sudoers",
        action="store_true",
        help="install the narrow sudoers drop-in needed for unattended system LaunchDaemon restarts",
    )
    parser.add_argument("--sudoers-user", default=None, help="user to grant in the generated sudoers drop-in")
    parser.add_argument(
        "--enqueue-detached",
        action="store_true",
        help=(
            "spawn the restart helper in a detached process and return immediately; "
            "use this when restarting the WebUI or gateway that owns the current session"
        ),
    )
    parser.add_argument("--detached-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--busy-snapshot-json",
        action="store_true",
        help=(
            "print the structured busy-state snapshot for --scope as JSON and "
            "exit; a read-only diagnostic, not a restart"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.busy_snapshot_json:
        print(json.dumps(busy_state_snapshot(args.scope), sort_keys=True))
        return 0
    if args.install_system_restart_sudoers:
        print(install_system_restart_sudoers(args.sudoers_user, dry_run=args.dry_run))
        return 0
    if args.describe:
        print(describe_plan(args.scope))
        return 0
    if args.enqueue_detached:
        notify_origin = None
        if args.notify_origin_json:
            notify_origin = json.loads(args.notify_origin_json)
        print(
            enqueue_detached_restart(
                args.scope,
                delay=args.delay,
                dry_run=args.dry_run,
                notify_origin=notify_origin,
                notify_tty=args.notify_tty,
                completion_marker=args.completion_marker,
                safe_wait_timeout=args.safe_wait_timeout,
                safe_wait_interval=args.safe_wait_interval,
            )
        )
        return 0
    if not args.detached_worker and not args.dry_run:
        parser.error(
            "refusing an inline restart; use --enqueue-detached so the restart "
            "worker survives termination of the invoking Hermes process"
        )
    try:
        return restart_scope(
            args.scope,
            delay=args.delay,
            dry_run=args.dry_run,
            notify_origin_json=args.notify_origin_json,
            notify_tty=args.notify_tty,
            completion_marker=args.completion_marker,
            safe_wait_timeout=args.safe_wait_timeout,
            safe_wait_interval=args.safe_wait_interval,
        )
    except Exception as exc:
        _append_log(f"restart helper crashed: {exc}")
        message = _completion_message(args.scope, 1)
        _notify_origin(args.notify_origin_json, message)
        _notify_tty(args.notify_tty, message)
        _write_completion_marker(args.completion_marker, args.scope, 1, message)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
