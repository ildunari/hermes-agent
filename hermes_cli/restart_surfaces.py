"""Detached Hermes surface restart helper.

This module exists so Telegram/Discord/CLI slash commands can hand off a
restart to a process that is not owned by the gateway being restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pwd
import shlex
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


@dataclass(frozen=True)
class RestartTarget:
    """One launchd target that can be restarted by the helper."""

    domain_template: str
    label: str
    required: bool = True
    description: str = ""

    def domain(self, uid: int) -> str:
        return self.domain_template.format(uid=uid)

    def service_name(self, uid: int) -> str:
        return f"{self.domain(uid)}/{self.label}"


GATEWAY_TARGETS: tuple[RestartTarget, ...] = (
    RestartTarget("user/{uid}", "ai.hermes.gateway", description="default Hermes gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-gpt", description="GPT Hermes gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-coding", required=False, description="coding profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-email-assistant", required=False, description="email-assistant profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-browser-agent", required=False, description="browser-agent profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-design", required=False, description="design profile gateway"),
    RestartTarget("user/{uid}", "ai.hermes.gateway-bookie", required=False, description="bookie profile gateway"),
    # Some profile LaunchAgents with LimitLoadToSessionType Aqua/Background load
    # into the gui domain instead of user. Keep both optional targets so a full
    # restart touches whichever domain launchd actually chose.
    RestartTarget("gui/{uid}", "ai.hermes.gateway-design", required=False, description="design profile gateway"),
    RestartTarget("gui/{uid}", "ai.hermes.gateway-bookie", required=False, description="bookie profile gateway"),
    # The WebUI/dashboard LaunchAgent owns the local dashboard backend on 9119.
    # It must move with /restart-gateways after smart updates; otherwise the
    # gateways can restart on new code while the dashboard keeps an old process.
    RestartTarget("user/{uid}", "ai.hermes.webui", description="Hermes WebUI/dashboard"),
    RestartTarget("system", "com.kosta.hermes-dashboard-system", required=False, description="dashboard backend on 9119"),
    RestartTarget("system", "com.kosta.hermes-dashboard-proxy-system", required=False, description="dashboard path proxy"),
    RestartTarget("user/{uid}", "ai.hermes.dashboard-host-rewrite-proxy", required=False, description="WebUI dashboard host rewrite proxy"),
    # Dedicated remote dashboard backend for MacBook Hermes Desktop.
    RestartTarget("user/{uid}", "ai.hermes.desktop-remote-dashboard", required=False, description="MacBook Hermes Desktop remote dashboard on 9120"),
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
    "gateways": (8642, 8643, 8644, 9119, 9120),
    "hermes": (8642, 8643, 8644, 8776, 8787, 9119, 9120, 9192, 3192, 3100),
}

GATEWAY_STATUS_PATHS: dict[str, Path] = {
    "ai.hermes.gateway": Path.home() / ".hermes" / "gateway_state.json",
    "ai.hermes.gateway-gpt": Path.home() / ".hermes" / "profiles" / "gpt" / "gateway_state.json",
    "ai.hermes.gateway-coding": Path.home() / ".hermes" / "profiles" / "coding" / "gateway_state.json",
    "ai.hermes.gateway-email-assistant": Path.home() / ".hermes" / "profiles" / "email-assistant" / "gateway_state.json",
    "ai.hermes.gateway-browser-agent": Path.home() / ".hermes" / "profiles" / "browser-agent" / "gateway_state.json",
    "ai.hermes.gateway-design": Path.home() / ".hermes" / "profiles" / "design" / "gateway_state.json",
    "ai.hermes.gateway-bookie": Path.home() / ".hermes" / "profiles" / "bookie" / "gateway_state.json",
}
# A queued restart should behave like a staged operation: if another Hermes
# session is still running, wait for it to drain instead of forcing Kosta to
# rerun the command manually.  Twenty-four hours keeps a stuck gateway from
# hiding a failed restart forever while covering normal long agent runs.
DEFAULT_SAFE_WAIT_TIMEOUT = 24 * 60 * 60
DEFAULT_SAFE_WAIT_INTERVAL = 2.0

# WebUI busy probe: the WebUI (ai.hermes.webui) hosts live chat turns whose
# worker state dies with the process. /health reports `active_runs` (worker
# runs independent of SSE attachment), so a restart that includes the WebUI
# target must wait until no runs are in flight — same contract as the
# gateway active_agents drain check.
WEBUI_BUSY_LABELS = frozenset({"ai.hermes.webui"})
WEBUI_HEALTH_URL = "http://127.0.0.1:8787/health"
WEBUI_HEALTH_TIMEOUT = 3.0


class RestartError(RuntimeError):
    pass


def normalize_scope(scope: str) -> str:
    raw = (scope or "").strip().lower().replace("_", "-")
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


def targets_for_scope(scope: str) -> tuple[RestartTarget, ...]:
    normalized = normalize_scope(scope)
    targets = GATEWAY_TARGETS if normalized == "gateways" else FULL_HERMES_TARGETS
    return _unique_targets(targets)


def system_restart_targets() -> tuple[RestartTarget, ...]:
    """Return unique system LaunchDaemons the restart helper may sudo-kick."""

    return tuple(target for target in _unique_targets(FULL_HERMES_TARGETS) if target.domain_template == "system")


def describe_plan(scope: str, *, uid: int | None = None) -> str:
    """Return a human-readable dry-run plan without changing runtime state."""

    normalized = normalize_scope(scope)
    uid = uid if uid is not None else os.getuid()
    command = "/restart-gateways" if normalized == "gateways" else "/restart-hermes"
    includes = (
        "default + GPT profile gateways, optional profile gateways, WebUI/dashboard"
        if normalized == "gateways"
        else "gateway restart scope plus Workspace, watchdog, Codex supervisor, proxies, voice bridge, and Claude-Hermes surfaces"
    )
    lines = [
        f"Restart scope: {normalized}",
        f"Canonical command: {command}",
        f"Includes: {includes}",
        "Launchd targets:",
    ]
    for target in targets_for_scope(normalized):
        req = "required" if target.required else "best-effort"
        desc = f" — {target.description}" if target.description else ""
        lines.append(f"- {target.service_name(uid)} ({req}){desc}")
    ports = VERIFY_PORTS.get(normalized, ())
    if ports:
        lines.append("Verification ports: " + ", ".join(str(p) for p in ports))
    lines.append(f"Log: {LOG_PATH}")
    return "\n".join(lines)


def _default_sudoers_user() -> str:
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name


def system_restart_sudoers_content(username: str | None = None) -> str:
    """Return the narrow sudoers drop-in for unattended system-service restarts."""

    user = username or _default_sudoers_user()
    if not user or any(ch.isspace() or ch in {":", ",", "=", "\\"} for ch in user):
        raise RestartError(f"Unsafe sudoers username: {user!r}")
    commands = ", ".join(
        f"{LAUNCHCTL_BIN} kickstart -k {target.service_name(os.getuid())}"
        for target in system_restart_targets()
    )
    return (
        "# Managed by Hermes Agent. Allows unattended restarts only for the\n"
        "# Hermes-owned system LaunchDaemons used by /restart-gateways and /restart-hermes.\n"
        f"Cmnd_Alias HERMES_RESTART_SURFACES = {commands}\n"
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
    """Report the WebUI as busy while it has live chat worker runs.

    Only consulted when the restart set actually includes a WebUI target.
    A dead/unreachable WebUI is NOT busy (restart should proceed and revive
    it); only a healthy server reporting active_runs > 0 blocks.
    """
    if not any(target.label in WEBUI_BUSY_LABELS for target in targets):
        return []
    try:
        import urllib.request

        with urllib.request.urlopen(WEBUI_HEALTH_URL, timeout=WEBUI_HEALTH_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []
    try:
        active_runs = int(payload.get("active_runs") or 0)
    except (TypeError, ValueError):
        active_runs = 0
    if active_runs > 0:
        oldest = payload.get("oldest_run_age_seconds")
        detail = f"ai.hermes.webui: active_runs={active_runs}"
        if oldest is not None:
            detail += f", oldest_run_age_seconds={oldest}"
        return [detail]
    return []


def _gateway_busy_details(targets: Iterable[RestartTarget]) -> list[str]:
    busy: list[str] = []
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
        if active_agents > 0 or (restart_requested and gateway_state == "draining"):
            busy.append(
                f"{target.label}: active_agents={active_agents}, state={gateway_state}, "
                f"restart_requested={restart_requested}, status={status_path}"
            )
    return busy


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
        last_busy = _gateway_busy_details(targets) + _webui_busy_details(targets)
        if not last_busy:
            _append_log("safe restart check passed")
            return True, []
        _append_log("safe restart waiting: " + " | ".join(last_busy))
        if time.monotonic() >= deadline:
            return False, last_busy
        time.sleep(max(0.1, interval))


def _completion_message(scope: str, exit_code: int) -> str:
    normalized = normalize_scope(scope)
    label = "Hermes gateways" if normalized == "gateways" else "Hermes surfaces"
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
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "complete",
            "scope": normalize_scope(scope),
            "exit_code": int(exit_code),
            "message": message,
            "completed_at": _timestamp(),
            "log_path": str(LOG_PATH),
        }
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        _append_log(f"completion marker wrote to {path}")
    except Exception as exc:
        _append_log(f"completion marker failed: {exc}")


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
    _append_log(f"restart requested scope={normalized} dry_run={dry_run} uid={uid}")
    _append_log(describe_plan(normalized, uid=uid).replace("\n", " | "))
    if dry_run:
        return 0
    if delay > 0:
        time.sleep(delay)

    failures: list[str] = []
    targets = targets_for_scope(normalized)
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

    for target in targets:
        service = target.service_name(uid)
        before = _launchctl_print(service)
        if before.returncode != 0:
            msg = f"{service} is not loaded"
            _append_log(msg)
            if target.required:
                failures.append(msg)
            continue
        kicked = _kickstart(service) if target.required else _kickstart_optional(target, service)
        if kicked.returncode != 0:
            msg = f"{service} restart failed"
            _append_log(msg)
            if target.required:
                failures.append(msg)
            continue
        time.sleep(0.4)
        after = _launchctl_print(service)
        if after.returncode != 0:
            msg = f"{service} did not verify after restart"
            _append_log(msg)
            if target.required:
                failures.append(msg)

    for port in VERIFY_PORTS.get(normalized, ()):
        _run(["bash", "-lc", f"lsof -nP -iTCP:{port} -sTCP:LISTEN >/dev/null"], timeout=10)

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
    """Spawn a detached helper process and return a user-facing status line."""

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
    notify_note = " I'll send a follow-up here when it finishes." if (notify_origin or notify_tty or completion_marker) else ""
    return (
        f"Queued detached Hermes {normalized} restart. "
        f"It will wait for active gateway tasks to finish before restarting."
        f"{notify_note} Log: {LOG_PATH}"
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restart Hermes launchd surfaces from a detached helper")
    parser.add_argument("--scope", default="gateways", choices=("gateways", "hermes"))
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
    args = parser.parse_args(list(argv) if argv is not None else None)
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
