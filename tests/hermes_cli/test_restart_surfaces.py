import json
import subprocess

import pytest

from hermes_cli.restart_surfaces import (
    RestartTarget,
    _gateway_pid as real_gateway_pid,
    _graceful_restart_gateway as real_graceful_restart_gateway,
    _webui_busy_details as real_webui_busy_details,
    describe_plan,
    enqueue_detached_restart,
    normalize_scope,
    restart_scope,
    targets_for_scope,
)


@pytest.fixture(autouse=True)
def _no_live_webui_probe(monkeypatch):
    """Keep orchestration tests isolated from the live WebUI and gateways."""
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._webui_busy_details",
        lambda _targets: [],
    )
    # Most scope tests exercise orchestration around gateways and must never
    # signal the live gateway PID found in this developer's status files.
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._graceful_restart_gateway",
        lambda *_args, **_kwargs: None,
    )


def test_gateway_scope_plan_includes_profile_gateway_domains():
    plan = describe_plan("gateways", uid=503)
    assert "user/503/ai.hermes.gateway" in plan
    assert "user/503/ai.hermes.gateway-gpt" in plan
    assert "user/503/ai.hermes.gateway-coding" in plan
    assert "user/503/ai.hermes.gateway-email-assistant" in plan
    assert "user/503/ai.hermes.gateway-browser-agent" in plan
    assert "user/503/ai.hermes.gateway-design" in plan
    assert "user/503/ai.hermes.gateway-bookie" in plan
    assert "user/503/ai.hermes.gateway-scientist" in plan
    assert "user/503/ai.hermes.gateway-poke" in plan
    assert "gui/503/ai.hermes.gateway-design" in plan
    assert "gui/503/ai.hermes.gateway-bookie" in plan
    assert "gui/503/ai.hermes.gateway-scientist" in plan
    assert "gui/503/ai.hermes.gateway-poke" in plan
    assert "user/503/ai.hermes.webui" in plan
    assert "system/com.kosta.hermes-dashboard-system" in plan
    assert "system/com.kosta.hermes-dashboard-proxy-system" in plan
    assert "user/503/ai.hermes.dashboard-host-rewrite-proxy" in plan
    assert "user/503/ai.hermes.desktop-remote-dashboard" in plan
    assert "Verification ports: 8642, 8643, 8644, 8787, 9119, 9120" in plan


def test_full_hermes_scope_includes_known_surfaces():
    labels = {target.label for target in targets_for_scope("hermes")}
    assert "ai.hermes.gateway" in labels
    assert "ai.hermes.gateway-gpt" in labels
    assert "ai.hermes.gateway-design" in labels
    assert "ai.hermes.gateway-bookie" in labels
    assert "ai.hermes.gateway-scientist" in labels
    assert "ai.hermes.gateway-poke" in labels
    assert "ai.hermes.webui" in labels
    assert "ai.hermes.desktop-remote-dashboard" in labels
    assert "ai.hermes.dashboard-host-rewrite-proxy" in labels
    assert "ai.hermes.workspace" in labels
    assert "ai.hermes.workspace-proxy" in labels
    assert "ai.hermes.watchdog" in labels
    assert "ai.hermes.codex-subtask-supervisor" in labels
    assert "com.kosta.hermes-gpt-api-proxy" in labels
    assert "com.kosta.clawket-hermes-gpt" in labels
    assert "com.kosta.hermes-side-bridge" in labels
    assert "com.kosta.hermes-voice-bridge" in labels
    assert "com.kosta.hermes-voice-bridge-system" in labels
    assert "com.kosta.hermes-workspace-system" in labels


def test_restart_scope_targets_are_unique():
    for scope in ("gateways", "hermes"):
        keys = [(target.domain_template, target.label) for target in targets_for_scope(scope)]
        assert len(keys) == len(set(keys))


def test_restart_scope_resolves_required_gateway_gui_alternate(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)
    calls = []
    graceful_services = []

    def fake_run(cmd, *, timeout=30):
        calls.append(cmd)

        class Proc:
            stdout = ""
            stderr = ""
            returncode = 0

        joined = "/".join(cmd)
        if cmd[:2] == ["/bin/launchctl", "print"] and "user/" in joined:
            Proc.returncode = 1
        elif cmd[:2] == ["/bin/launchctl", "print"] and "gui/" in joined:
            Proc.returncode = 0
        elif cmd[:3] == ["/bin/launchctl", "kickstart", "-k"]:
            assert cmd[-1].startswith("gui/")
            Proc.returncode = 0
        return Proc()

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "targets_for_scope", lambda _scope: (target,))
    monkeypatch.setattr(restart_surfaces, "VERIFY_PORTS", {"hermes": ()})
    monkeypatch.setattr(restart_surfaces, "_gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr(restart_surfaces, "_webui_busy_details", lambda _targets: [])
    monkeypatch.setattr(restart_surfaces, "_run", fake_run)
    monkeypatch.setattr(
        restart_surfaces,
        "_graceful_restart_gateway",
        lambda _target, service, _before, **_kwargs: graceful_services.append(service) or None,
    )
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert restart_surfaces.restart_scope("hermes", delay=0) == 0
    assert any("gui/" in "/".join(cmd) for cmd in calls)
    assert graceful_services == [f"gui/{restart_surfaces.os.getuid()}/ai.hermes.gateway"]
    assert not any(cmd[:3] == ["/bin/launchctl", "kickstart", "-k"] for cmd in calls)


def test_gateway_pid_requires_validated_status_and_launchd_agreement(monkeypatch):
    from gateway import status as gateway_status
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)
    before = subprocess.CompletedProcess(
        ["launchctl", "print"],
        0,
        stdout="\tpid = 222\n",
        stderr="",
    )
    monkeypatch.setattr(restart_surfaces, "_read_json", lambda _path: {"pid": 111})
    monkeypatch.setattr(gateway_status, "get_runtime_status_running_pid", lambda *_a, **_k: 111)
    assert real_gateway_pid(target, before) is None

    monkeypatch.setattr(gateway_status, "get_runtime_status_running_pid", lambda *_a, **_k: 222)
    assert real_gateway_pid(target, before) == 222


def test_graceful_gateway_restart_signals_and_waits_for_replacement(monkeypatch):
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)
    before = subprocess.CompletedProcess(
        ["launchctl", "print"],
        0,
        stdout="\tpid = 100\n",
        stderr="",
    )
    signals = []
    monkeypatch.setattr(restart_surfaces, "_gateway_pid", lambda *_args: 100)
    monkeypatch.setattr(restart_surfaces, "_read_json", lambda _path: {"pid": 101})
    monkeypatch.setattr(restart_surfaces, "_pid_is_alive", lambda pid: pid == 101)
    monkeypatch.setattr(restart_surfaces.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    failure = real_graceful_restart_gateway(
        target,
        "user/503/ai.hermes.gateway",
        before,
        timeout=10,
    )

    assert failure is None
    assert signals == [(100, restart_surfaces.signal.SIGUSR1)]


def test_restart_scope_fails_when_verify_port_missing(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)

    def fake_run(cmd, *, timeout=30):
        class Proc:
            stdout = ""
            stderr = ""
            returncode = 0

        if cmd[:2] == ["bash", "-lc"] and "lsof" in cmd[-1]:
            Proc.returncode = 1
            Proc.stdout = ""
        return Proc()

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "targets_for_scope", lambda _scope: (target,))
    # 8642 is a REQUIRED gateway port (not in BEST_EFFORT_VERIFY_PORTS), so a
    # missing listener must fail the restart.
    monkeypatch.setattr(restart_surfaces, "VERIFY_PORTS", {"hermes": (8642,)})
    monkeypatch.setattr(restart_surfaces, "_gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr(restart_surfaces, "_webui_busy_details", lambda _targets: [])
    monkeypatch.setattr(restart_surfaces, "_run", fake_run)
    # Collapse the bounded health-wait so a persistent required failure returns
    # immediately instead of polling to the real-time deadline.
    monkeypatch.setattr(restart_surfaces, "DEFAULT_HEALTH_WAIT_TIMEOUT", 0.0)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert restart_surfaces.restart_scope("hermes", delay=0) == 1
    assert "port 8642 is not listening" in (tmp_path / "restart.log").read_text()


def test_restart_scope_tolerates_best_effort_dashboard_lag(monkeypatch, tmp_path):
    """A slow/unresponsive best-effort dashboard (9119/9120) must NOT fail an
    otherwise-healthy restart. The dashboard LaunchDaemon uses ThrottleInterval
    =30 and can lag its HTTP handler behind the listening socket; treating that
    as a hard failure marked a clean restart as exit 1 (the false-negative bug).
    """
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)

    def fake_run(cmd, *, timeout=30):
        class Proc:
            stdout = ""
            stderr = ""
            returncode = 0

        # Only the 9119 dashboard probes fail; every required check passes.
        if cmd[:2] == ["bash", "-lc"]:
            script = cmd[-1]
            if "9119" in script:
                Proc.returncode = 1
                Proc.stdout = "000"
        return Proc()

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "targets_for_scope", lambda _scope: (target,))
    # Required 8642 (healthy) alongside best-effort 9119 (failing probes).
    monkeypatch.setattr(restart_surfaces, "VERIFY_PORTS", {"hermes": (8642, 9119)})
    monkeypatch.setattr(restart_surfaces, "_gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr(restart_surfaces, "_webui_busy_details", lambda _targets: [])
    monkeypatch.setattr(restart_surfaces, "_run", fake_run)
    monkeypatch.setattr(restart_surfaces, "DEFAULT_HEALTH_WAIT_TIMEOUT", 0.0)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    # Restart succeeds despite the dashboard lag...
    assert restart_surfaces.restart_scope("hermes", delay=0) == 0
    log = (tmp_path / "restart.log").read_text()
    # ...and the lag is surfaced as a best-effort warning, not a hard failure.
    assert "best-effort" in log
    assert "9119" in log


def test_health_wait_covers_one_launchd_throttled_retry():
    from hermes_cli import restart_surfaces

    # com.kosta.hermes-dashboard-system has ThrottleInterval=30. The bounded
    # readiness window must permit one failed launch and its delayed retry.
    assert restart_surfaces.DEFAULT_HEALTH_WAIT_TIMEOUT > 30


def test_describe_plan_names_canonical_command_and_scope_summary():
    plan = describe_plan("gateways", uid=503)
    assert "Canonical command: /restart-gateways" in plan
    assert "Includes: default + GPT profile gateways" in plan


def test_enqueue_dry_run_does_not_spawn(monkeypatch):
    def fail_popen(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("dry-run should not spawn a helper")

    monkeypatch.setattr("subprocess.Popen", fail_popen)
    output = enqueue_detached_restart("restart-gateways", dry_run=True)
    assert "Restart scope: gateways" in output
    assert "ai.hermes.gateway-gpt" in output


def test_restart_scope_dry_run_does_not_call_launchctl(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, *, timeout=30):
        calls.append(cmd)
        raise AssertionError("dry-run should not call launchctl")

    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr("hermes_cli.restart_surfaces._run", fake_run)
    assert restart_scope("gateways", dry_run=True) == 0
    assert calls == []


def test_scope_aliases():
    assert normalize_scope("restart-gateways") == "gateways"
    assert normalize_scope("all") == "hermes"


def test_system_restart_sudoers_content_is_narrow():
    from hermes_cli import restart_surfaces

    content = restart_surfaces.system_restart_sudoers_content("Kosta")

    assert "Kosta ALL=(root) NOPASSWD: HERMES_RESTART_SURFACES" in content
    assert "/bin/launchctl kickstart -k system/com.kosta.hermes-dashboard-system" in content
    assert "/bin/launchctl kickstart -k system/com.kosta.hermes-workspace-proxy-system" in content
    assert "ALL" not in content.split("Cmnd_Alias HERMES_RESTART_SURFACES = ", 1)[1].split("\n", 1)[0]
    assert "bootout" not in content
    assert "unload" not in content


def test_cli_can_print_system_restart_sudoers_dry_run(capsys):
    from hermes_cli.restart_surfaces import main

    assert main(["--install-system-restart-sudoers", "--dry-run", "--sudoers-user", "Kosta"]) == 0
    output = capsys.readouterr().out
    assert "Cmnd_Alias HERMES_RESTART_SURFACES" in output
    assert "Kosta ALL=(root) NOPASSWD" in output


def test_enqueue_restart_can_request_webui_completion_marker(monkeypatch, tmp_path):
    launched = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched["cmd"] = cmd
            launched["kwargs"] = kwargs

    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    marker = tmp_path / "status.json"

    output = enqueue_detached_restart("gateways", completion_marker=str(marker))

    assert "follow-up here" not in output
    cmd = launched["cmd"]
    assert "--detached-worker" in cmd
    assert launched["kwargs"]["stdin"] is subprocess.DEVNULL
    assert launched["kwargs"]["start_new_session"] is True
    assert launched["kwargs"]["close_fds"] is True
    marker_index = cmd.index("--completion-marker") + 1
    assert cmd[marker_index] == str(marker)


def test_enqueue_restart_can_request_origin_completion_notification(monkeypatch, tmp_path):
    launched = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched["cmd"] = cmd
            launched["kwargs"] = kwargs

    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    origin = {"platform": "telegram", "chat_id": "123", "thread_id": "456"}

    output = enqueue_detached_restart("gateways", notify_origin=origin)

    assert "follow-up here" in output
    cmd = launched["cmd"]
    notify_index = cmd.index("--notify-origin-json") + 1
    assert json.loads(cmd[notify_index]) == origin


def test_cli_enqueue_detached_uses_safe_launcher(monkeypatch, capsys, tmp_path):
    launched = {}

    def fake_enqueue(scope, **kwargs):
        launched["scope"] = scope
        launched["kwargs"] = kwargs
        return "queued"

    monkeypatch.setattr("hermes_cli.restart_surfaces.enqueue_detached_restart", fake_enqueue)
    from hermes_cli.restart_surfaces import main

    marker = tmp_path / "restart.json"
    assert main([
        "--scope", "hermes",
        "--delay", "0",
        "--completion-marker", str(marker),
        "--safe-wait-timeout", "12",
        "--safe-wait-interval", "0.5",
        "--enqueue-detached",
    ]) == 0

    assert capsys.readouterr().out.strip() == "queued"
    assert launched["scope"] == "hermes"
    assert launched["kwargs"]["delay"] == 0
    assert launched["kwargs"]["completion_marker"] == str(marker)
    assert launched["kwargs"]["safe_wait_timeout"] == 12
    assert launched["kwargs"]["safe_wait_interval"] == 0.5


def test_cli_refuses_inline_restart(capsys):
    from hermes_cli.restart_surfaces import main

    with pytest.raises(SystemExit) as exc:
        main(["--scope", "hermes"])
    assert exc.value.code == 2
    assert "refusing an inline restart" in capsys.readouterr().err


def test_enqueue_restart_can_stage_until_sessions_drain(monkeypatch, tmp_path):
    launched = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched["cmd"] = cmd
            launched["kwargs"] = kwargs

    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr("subprocess.Popen", FakePopen)

    output = enqueue_detached_restart(
        "hermes",
        safe_wait_timeout=24 * 60 * 60,
        safe_wait_interval=2.0,
    )

    assert "wait for active gateway tasks" in output
    cmd = launched["cmd"]
    timeout_index = cmd.index("--safe-wait-timeout") + 1
    interval_index = cmd.index("--safe-wait-interval") + 1
    assert cmd[timeout_index] == str(24 * 60 * 60)
    assert cmd[interval_index] == "2.0"


def test_restart_scope_waits_for_active_gateway_tasks_before_launchctl(monkeypatch, tmp_path):
    calls = []
    busy_sequence = [
        ["ai.hermes.gateway-gpt: active_agents=1"],
        [],
    ]

    def fake_busy(_targets):
        return busy_sequence.pop(0)

    def fake_run(cmd, *, timeout=30):
        calls.append(cmd)
        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""
        return Proc()

    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr("hermes_cli.restart_surfaces._gateway_busy_details", fake_busy)
    monkeypatch.setattr("hermes_cli.restart_surfaces._webui_busy_details", lambda _targets: [])
    monkeypatch.setattr("hermes_cli.restart_surfaces._run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert restart_scope("gateways", delay=0, safe_wait_timeout=10, safe_wait_interval=0.1) == 0
    assert calls
    assert calls[0][:2] == ["/bin/launchctl", "print"]


def test_restart_scope_times_out_before_launchctl_when_gateway_stays_busy(monkeypatch, tmp_path):
    calls = []
    notifications = []

    def fake_run(cmd, *, timeout=30):
        calls.append(cmd)
        raise AssertionError("launchctl should not run while gateway is busy")

    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._gateway_busy_details",
        lambda _targets: ["ai.hermes.gateway: active_agents=1"],
    )
    monkeypatch.setattr("hermes_cli.restart_surfaces._run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._notify_tty",
        lambda tty, message: notifications.append((tty, message)),
    )

    assert restart_scope(
        "gateways",
        delay=0,
        safe_wait_timeout=0,
        safe_wait_interval=0.1,
        notify_tty="/dev/ttys001",
    ) == 1
    assert calls == []
    assert "finished with errors" in notifications[0][1]


def test_run_converts_subprocess_timeout_to_failed_process(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["/usr/bin/sudo", "-n", "/bin/launchctl"], timeout=3)

    monkeypatch.setattr(subprocess, "run", fake_run)

    proc = restart_surfaces._run(["/usr/bin/sudo", "-n", "/bin/launchctl"], timeout=3)

    assert proc.returncode == 124
    assert "timed out after 3s" in proc.stderr
    assert "exit=124" in (tmp_path / "restart.log").read_text()


def test_optional_system_targets_try_noninteractive_sudo_without_modifying_plists(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    calls = []

    def fake_run(cmd, *, timeout=30):
        calls.append(cmd)

        class Proc:
            returncode = 0 if cmd[:2] == ["/bin/launchctl", "print"] else 1
            stdout = ""
            stderr = "operation not permitted"

        return Proc()

    optional_system = RestartTarget(
        "system",
        "com.kosta.hermes-workspace-system",
        required=False,
        description="workspace backend",
    )
    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "targets_for_scope", lambda _scope: (optional_system,))
    monkeypatch.setattr(restart_surfaces, "VERIFY_PORTS", {"hermes": ()})
    monkeypatch.setattr(restart_surfaces, "_gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr(restart_surfaces, "_run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert restart_surfaces.restart_scope("hermes", delay=0) == 0

    assert ["/bin/launchctl", "kickstart", "-k", "system/com.kosta.hermes-workspace-system"] in calls
    assert ["/usr/bin/sudo", "-n", "/bin/launchctl", "kickstart", "-k", "system/com.kosta.hermes-workspace-system"] in calls
    flattened = "\n".join(" ".join(cmd) for cmd in calls)
    assert "bootout" not in flattened
    assert "unload" not in flattened
    assert "remove" not in flattened
    assert "sudo was required but unavailable non-interactively" in (tmp_path / "restart.log").read_text()


def test_required_system_target_uses_sudo_fallback_and_reports_timeout(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    calls = []

    def fake_run(cmd, *, timeout=30):
        calls.append(cmd)

        class Proc:
            stdout = ""
            stderr = ""

        proc = Proc()
        if cmd[:2] == ["/bin/launchctl", "print"]:
            proc.returncode = 0
        elif cmd == ["/bin/launchctl", "kickstart", "-k", "system/com.kosta.required"]:
            proc.returncode = 1
            proc.stderr = "operation not permitted"
        elif cmd == ["/usr/bin/sudo", "-n", "/bin/launchctl", "kickstart", "-k", "system/com.kosta.required"]:
            proc.returncode = 124
            proc.stderr = "timed out after 30s"
        else:
            proc.returncode = 0
        return proc

    required_system = RestartTarget("system", "com.kosta.required", required=True)
    marker = tmp_path / "status.json"
    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "targets_for_scope", lambda _scope: (required_system,))
    monkeypatch.setattr(restart_surfaces, "VERIFY_PORTS", {"hermes": ()})
    monkeypatch.setattr(restart_surfaces, "_gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr(restart_surfaces, "_run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert restart_surfaces.restart_scope("hermes", delay=0, completion_marker=str(marker)) == 1

    assert ["/usr/bin/sudo", "-n", "/bin/launchctl", "kickstart", "-k", "system/com.kosta.required"] in calls
    payload = json.loads(marker.read_text())
    assert payload["exit_code"] == 1
    assert "finished with errors" in payload["message"]


def test_gateway_busy_details_ignores_stale_dead_status(monkeypatch, tmp_path):
    status = tmp_path / "gateway_state.json"
    status.write_text(json.dumps({"pid": 123, "active_agents": 2, "gateway_state": "running"}))
    target = targets_for_scope("gateways")[0]

    monkeypatch.setattr(
        "hermes_cli.restart_surfaces.GATEWAY_STATUS_PATHS",
        {target.label: status},
    )
    monkeypatch.setattr("hermes_cli.restart_surfaces._pid_is_alive", lambda _pid: False)

    from hermes_cli.restart_surfaces import _gateway_busy_details

    assert _gateway_busy_details([target]) == []


def test_restart_scope_sends_completion_notifications(monkeypatch, tmp_path):
    calls = []
    notifications = []

    def fake_run(cmd, *, timeout=30):
        calls.append(cmd)
        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""
        return Proc()

    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr("hermes_cli.restart_surfaces._run", fake_run)
    monkeypatch.setattr("hermes_cli.restart_surfaces._gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._notify_origin",
        lambda origin, message: notifications.append(("origin", origin, message)),
    )
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._notify_tty",
        lambda tty, message: notifications.append(("tty", tty, message)),
    )

    assert restart_scope(
        "gateways",
        notify_origin_json='{"platform":"telegram","chat_id":"123"}',
        notify_tty="/dev/ttys001",
    ) == 0

    assert any("Hermes gateways restart finished" in item[2] for item in notifications)
    assert notifications[0][0] == "origin"
    assert notifications[1][0] == "tty"


def test_restart_scope_writes_completion_marker(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, *, timeout=30):
        calls.append(cmd)
        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""
        return Proc()

    marker = tmp_path / "status.json"
    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr("hermes_cli.restart_surfaces._run", fake_run)
    monkeypatch.setattr("hermes_cli.restart_surfaces._gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert restart_scope("gateways", delay=0, completion_marker=str(marker)) == 0

    payload = json.loads(marker.read_text())
    assert payload["status"] == "complete"
    assert payload["scope"] == "gateways"
    assert payload["exit_code"] == 0
    assert "Hermes gateways restart finished" in payload["message"]
    assert marker.stat().st_mode & 0o777 == 0o600
    assert not list(marker.parent.glob(f".{marker.name}.*"))


def test_main_reports_completion_when_restart_scope_crashes(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    marker = tmp_path / "status.json"
    notifications = []
    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(restart_surfaces, "restart_scope", boom)
    monkeypatch.setattr(
        restart_surfaces,
        "_notify_origin",
        lambda origin, message: notifications.append(("origin", origin, message)),
    )
    monkeypatch.setattr(
        restart_surfaces,
        "_notify_tty",
        lambda tty, message: notifications.append(("tty", tty, message)),
    )

    assert restart_surfaces.main([
        "--scope", "gateways",
        "--completion-marker", str(marker),
        "--notify-origin-json", '{"platform":"telegram","chat_id":"123"}',
        "--notify-tty", "/dev/ttys001",
        "--detached-worker",
    ]) == 1

    payload = json.loads(marker.read_text())
    assert payload["status"] == "complete"
    assert payload["exit_code"] == 1
    assert "finished with errors" in payload["message"]
    assert notifications[0] == ("origin", '{"platform":"telegram","chat_id":"123"}', payload["message"])
    assert notifications[1] == ("tty", "/dev/ttys001", payload["message"])


def _webui_target():
    return RestartTarget("user/{uid}", "ai.hermes.webui", description="Hermes WebUI/dashboard")


def test_webui_busy_details_skipped_when_no_webui_target(monkeypatch):
    from hermes_cli import restart_surfaces

    def boom(*_args, **_kwargs):
        raise AssertionError("health probe must not run without a WebUI target")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    gateway_only = [RestartTarget("user/{uid}", "ai.hermes.gateway")]
    assert restart_surfaces._webui_busy_details(gateway_only) == []


def test_webui_busy_details_reports_active_runs(monkeypatch):
    import io

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    payload = json.dumps({"active_runs": 2, "oldest_run_age_seconds": 41.5}).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: FakeResponse(payload),
    )
    busy = real_webui_busy_details([_webui_target()])
    assert busy == ["ai.hermes.webui: active_runs=2, oldest_run_age_seconds=41.5"]


def test_webui_busy_details_idle_and_unreachable_fail_closed(monkeypatch):
    import io

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    idle = json.dumps({"active_runs": 0}).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: FakeResponse(idle),
    )
    assert real_webui_busy_details([_webui_target()]) == []

    def refuse(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    busy = real_webui_busy_details([_webui_target()])
    assert len(busy) == 1
    assert "health probe unavailable or invalid" in busy[0]


@pytest.mark.parametrize("active_runs", [0.5, -0.5, True, "0"])
def test_webui_busy_details_rejects_noninteger_active_runs(monkeypatch, active_runs):
    import io

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    payload = json.dumps({"active_runs": active_runs}).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: FakeResponse(payload),
    )

    busy = real_webui_busy_details([_webui_target()])
    assert len(busy) == 1
    assert "health probe unavailable or invalid" in busy[0]


def test_restart_scope_waits_for_webui_active_runs_before_launchctl(monkeypatch, tmp_path):
    calls = []
    webui_busy_sequence = [
        ["ai.hermes.webui: active_runs=1"],
        [],
        [],
    ]

    def fake_run(cmd, *, timeout=30):
        calls.append(cmd)

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr("hermes_cli.restart_surfaces._gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._webui_busy_details",
        lambda _targets: webui_busy_sequence.pop(0),
    )
    monkeypatch.setattr("hermes_cli.restart_surfaces._run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert restart_scope("hermes", delay=0, safe_wait_timeout=10, safe_wait_interval=0.1) == 0
    assert calls


def test_restart_scope_rechecks_webui_immediately_before_kick(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    events = []
    webui_target = _webui_target()
    targets = (
        RestartTarget("user/{uid}", "ai.hermes.gateway", required=True),
        webui_target,
        RestartTarget("user/{uid}", "ai.hermes.after-webui", required=True),
    )
    probe_sequence = [
        [],  # Initial scope-level drain.
        ["ai.hermes.webui: active_runs=1"],  # Work began before WebUI was reached.
        [],  # The new run finished; the final boundary is now safe.
    ]

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_webui_busy(_targets):
        events.append("probe")
        return probe_sequence.pop(0)

    def fake_kickstart(service):
        events.append(f"kick:{service.rsplit('/', 1)[-1]}")
        return Proc()

    monkeypatch.setattr("hermes_cli.restart_surfaces.LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr("hermes_cli.restart_surfaces.targets_for_scope", lambda _scope: targets)
    monkeypatch.setattr("hermes_cli.restart_surfaces._gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr("hermes_cli.restart_surfaces._webui_busy_details", fake_webui_busy)
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._resolve_loaded_service",
        lambda service: (service, Proc()),
    )
    monkeypatch.setattr("hermes_cli.restart_surfaces._kickstart", fake_kickstart)
    monkeypatch.setattr("hermes_cli.restart_surfaces._launchctl_print", lambda _service: Proc())
    monkeypatch.setattr("hermes_cli.restart_surfaces._wait_for_scope_health", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert restart_scope("hermes", delay=0, safe_wait_timeout=10, safe_wait_interval=0.1) == 0
    assert probe_sequence == []
    assert events == [
        "probe",
        "probe",
        "probe",
        "kick:ai.hermes.webui",
        "kick:ai.hermes.after-webui",
    ]
