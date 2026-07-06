import json

from hermes_cli.restart_surfaces import (
    describe_plan,
    enqueue_detached_restart,
    normalize_scope,
    restart_scope,
    targets_for_scope,
)


def test_gateway_scope_plan_includes_user_domain_and_bookie_gui_domain():
    plan = describe_plan("gateways", uid=503)
    assert "user/503/ai.hermes.gateway" in plan
    assert "user/503/ai.hermes.gateway-gpt" in plan
    assert "user/503/ai.hermes.gateway-coding" in plan
    assert "user/503/ai.hermes.gateway-email-assistant" in plan
    assert "user/503/ai.hermes.gateway-browser-agent" in plan
    assert "user/503/ai.hermes.gateway-bookie" in plan
    assert "gui/503/ai.hermes.gateway-bookie" in plan
    assert "user/503/ai.hermes.webui" in plan
    assert "system/com.kosta.hermes-dashboard-system" in plan
    assert "system/com.kosta.hermes-dashboard-proxy-system" in plan
    assert "user/503/ai.hermes.dashboard-host-rewrite-proxy" in plan
    assert "user/503/ai.hermes.desktop-remote-dashboard" in plan
    assert "Verification ports: 8642, 8643, 8644, 9119, 9120" in plan


def test_full_hermes_scope_includes_known_surfaces():
    labels = {target.label for target in targets_for_scope("hermes")}
    assert "ai.hermes.gateway" in labels
    assert "ai.hermes.gateway-gpt" in labels
    assert "ai.hermes.gateway-bookie" in labels
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

    assert "follow-up here" in output
    cmd = launched["cmd"]
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
    monkeypatch.setattr("hermes_cli.restart_surfaces._run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    assert restart_scope("gateways", delay=0, safe_wait_timeout=10, safe_wait_interval=0.1) == 0
    assert calls
    assert calls[0][:2] == ["launchctl", "print"]


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


def test_main_writes_completion_marker_when_restart_scope_crashes(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    marker = tmp_path / "status.json"
    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(restart_surfaces, "restart_scope", boom)

    try:
        restart_surfaces.main(["--scope", "gateways", "--completion-marker", str(marker)])
    except RuntimeError:
        pass
    else:  # pragma: no cover - should not happen
        raise AssertionError("main should propagate restart_scope errors")

    payload = json.loads(marker.read_text())
    assert payload["status"] == "complete"
    assert payload["exit_code"] == 1
    assert "finished with errors" in payload["message"]
