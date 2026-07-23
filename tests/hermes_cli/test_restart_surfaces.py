import json
import plistlib
import subprocess

import pytest

from hermes_cli.restart_surfaces import (
    BootstrapPolicy,
    RestartTarget,
    RestartVerification,
    _desktop_busy_details as real_desktop_busy_details,
    _gateway_pid as real_gateway_pid,
    _graceful_restart_gateway as real_graceful_restart_gateway,
    _multiplex_gateway_config as real_multiplex_gateway_config,
    _webui_busy_details as real_webui_busy_details,
    describe_plan,
    enqueue_detached_restart,
    normalize_scope,
    restart_scope,
    targets_for_scope,
    verification_ports_for_scope,
)


@pytest.fixture(autouse=True)
def _no_live_webui_probe(monkeypatch):
    """Keep orchestration tests isolated from the live WebUI and gateways."""
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._webui_busy_details",
        lambda _targets: [],
    )
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._desktop_busy_details",
        lambda _targets: [],
    )
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._desktop_dashboard_is_running",
        lambda: True,
    )
    # Most scope tests exercise orchestration around gateways and must never
    # signal the live gateway PID found in this developer's status files.
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._graceful_restart_gateway",
        lambda *_args, **_kwargs: (RestartVerification.RESTARTED, None),
    )
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._multiplex_gateway_config",
        lambda: None,
    )


def _multiplex_config(*, api_port=8642, webhook_port=8644, bluebubbles_port=8647):
    return {
        "multiplex_profiles": True,
        "platforms": {
            "api_server": {"enabled": True, "port": api_port},
            "webhook": {"enabled": True, "port": webhook_port},
            "bluebubbles": {
                "enabled": True,
                "extra": {
                    "webhook_register": True,
                    "webhook_port": bluebubbles_port,
                },
            },
        },
    }


def _write_launchd_plist(path, label, *, session_types=None):
    payload = {"Label": label}
    if session_types is not None:
        payload["LimitLoadToSessionType"] = session_types
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(payload))


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


def test_multiplex_gateway_scope_plan_uses_single_root_topology(monkeypatch):
    from hermes_cli import restart_surfaces

    monkeypatch.setattr(
        restart_surfaces,
        "_multiplex_gateway_config",
        lambda: _multiplex_config(),
    )

    plan = describe_plan("gateways", uid=503)

    assert "Includes: single root multiplex gateway, WebUI/dashboard" in plan
    assert "never auto-start named profile gateways" in plan
    assert "user/503/ai.hermes.gateway (required)" in plan
    assert "user/gui twins resolved at execution" in plan
    assert "user/503/ai.hermes.webui" in plan
    assert "system/com.kosta.hermes-dashboard-system" in plan
    assert "user/503/ai.hermes.dashboard-host-rewrite-proxy" in plan
    assert "user/503/ai.hermes.desktop-remote-dashboard" in plan
    assert "ai.hermes.gateway-gpt" not in plan
    assert "ai.hermes.gateway-coding" not in plan
    assert "ai.hermes.gateway-design" not in plan
    assert "ai.hermes.gateway-poke" not in plan
    assert "Verification ports: 8642, 8644, 8647, 8787, 9119, 9120" in plan


def test_multiplex_gateway_listener_ports_follow_merged_config(monkeypatch):
    from hermes_cli import restart_surfaces

    monkeypatch.setattr(
        restart_surfaces,
        "_multiplex_gateway_config",
        lambda: _multiplex_config(
            api_port=19642,
            webhook_port=19644,
            bluebubbles_port=19647,
        ),
    )

    assert verification_ports_for_scope("gateways") == (
        19642,
        19644,
        19647,
        8787,
        9119,
        9120,
    )


def test_multiplex_topology_probe_uses_readonly_config_without_gateway_discovery(
    monkeypatch,
):
    config = _multiplex_config()

    monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"multiplex_profiles": True},
    )
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: pytest.fail("restart dry-run must not run gateway/plugin discovery"),
    )

    assert real_multiplex_gateway_config() is config


def test_multiplex_restart_bootstraps_required_services_before_drain(
    monkeypatch,
    tmp_path,
):
    from hermes_cli import restart_surfaces

    uid = restart_surfaces.os.getuid()
    targets = (
        RestartTarget(
            "user/{uid}",
            "ai.hermes.gateway",
            bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
        ),
        RestartTarget(
            "user/{uid}",
            "ai.hermes.webui",
            bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
        ),
    )
    launch_agents = tmp_path / "LaunchAgents"
    for target in targets:
        _write_launchd_plist(launch_agents / f"{target.label}.plist", target.label)

    loaded = set()
    events = []

    def fake_run(cmd, *, timeout=30):
        if cmd[:2] == ["/bin/launchctl", "print"]:
            service = cmd[2]
            return subprocess.CompletedProcess(
                cmd,
                0 if service in loaded else 1,
                stdout="\tstate = running\n\tpid = 400\n" if service in loaded else "",
                stderr="" if service in loaded else "Could not find service",
            )
        if cmd[:2] == ["/bin/launchctl", "bootstrap"]:
            label = cmd[3].rsplit("/", 1)[-1].removesuffix(".plist")
            loaded.add(f"{cmd[2]}/{label}")
            events.append(f"bootstrap:{label}")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["/bin/launchctl", "kickstart", "-k"]:
            events.append(f"kick:{cmd[-1].rsplit('/', 1)[-1]}")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    def fake_drain(_targets, **_kwargs):
        assert f"user/{uid}/ai.hermes.gateway" in loaded
        assert f"user/{uid}/ai.hermes.webui" in loaded
        events.append("drain")
        return True, []

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "USER_LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setattr(restart_surfaces, "_multiplex_gateway_config", _multiplex_config)
    monkeypatch.setattr(restart_surfaces, "_targets_for_scope", lambda _scope, _config: targets)
    monkeypatch.setattr(restart_surfaces, "_verification_ports_for_scope", lambda *_args: ())
    monkeypatch.setattr(restart_surfaces, "_run", fake_run)
    monkeypatch.setattr(restart_surfaces, "_wait_for_safe_restart", fake_drain)
    monkeypatch.setattr(
        restart_surfaces,
        "_graceful_restart_gateway",
        lambda *_args, **_kwargs: (RestartVerification.RESTARTED, None),
    )
    monkeypatch.setattr(
        restart_surfaces,
        "_wait_for_scope_health",
        lambda *_args, **_kwargs: ([], []),
    )
    monkeypatch.setattr(restart_surfaces.time, "sleep", lambda *_args: None)

    assert restart_surfaces.restart_scope("gateways", delay=0) == 0
    assert events[:3] == [
        "bootstrap:ai.hermes.gateway",
        "bootstrap:ai.hermes.webui",
        "drain",
    ]


def test_named_profile_gateway_is_never_bootstrapped(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    named = next(
        target
        for target in restart_surfaces.GATEWAY_TARGETS
        if target.label == "ai.hermes.gateway-poke" and target.domain_template.startswith("user/")
    )
    launch_agents = tmp_path / "LaunchAgents"
    _write_launchd_plist(launch_agents / f"{named.label}.plist", named.label)

    monkeypatch.setattr(restart_surfaces, "USER_LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setattr(
        restart_surfaces,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("named profile must not be probed or bootstrapped"),
    )

    assert named.bootstrap_policy is BootstrapPolicy.NEVER
    assert restart_surfaces._bootstrap_targets_before_drain(
        (named,),
        uid=503,
        enabled=True,
    ) == []


def test_nonmultiplex_restart_never_bootstraps_configured_targets(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    target = RestartTarget(
        "user/{uid}",
        "ai.hermes.gateway",
        bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
    )
    launch_agents = tmp_path / "LaunchAgents"
    _write_launchd_plist(launch_agents / f"{target.label}.plist", target.label)
    monkeypatch.setattr(restart_surfaces, "USER_LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setattr(
        restart_surfaces,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("legacy topology must not bootstrap"),
    )

    assert restart_surfaces._bootstrap_targets_before_drain(
        (target,),
        uid=503,
        enabled=False,
    ) == []


def test_bootstrap_prefers_gui_for_aqua_launchagent(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    target = RestartTarget(
        "user/{uid}",
        "ai.hermes.webui",
        bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
    )
    launch_agents = tmp_path / "LaunchAgents"
    _write_launchd_plist(
        launch_agents / f"{target.label}.plist",
        target.label,
        session_types=["Aqua", "Background"],
    )
    loaded = set()
    bootstrap_domains = []

    def fake_run(cmd, *, timeout=30):
        if cmd[:2] == ["/bin/launchctl", "print"]:
            return subprocess.CompletedProcess(
                cmd,
                0 if cmd[2] in loaded else 1,
                stdout="\tstate = running\n\tpid = 500\n" if cmd[2] in loaded else "",
                stderr="",
            )
        if cmd[:2] == ["/bin/launchctl", "bootstrap"]:
            bootstrap_domains.append(cmd[2])
            loaded.add(f"{cmd[2]}/{target.label}")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "USER_LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setattr(restart_surfaces, "_run", fake_run)
    monkeypatch.setattr(restart_surfaces.time, "sleep", lambda *_args: None)

    assert restart_surfaces._bootstrap_targets_before_drain(
        (target,),
        uid=503,
        enabled=True,
    ) == []
    assert bootstrap_domains == ["gui/503"]


def test_system_bootstrap_uses_narrow_noninteractive_sudo_fallback(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    target = RestartTarget(
        "system",
        "com.kosta.hermes-dashboard-system",
        required=False,
        bootstrap_policy=BootstrapPolicy.MULTIPLEX_CONFIGURED,
    )
    launch_daemons = tmp_path / "LaunchDaemons"
    plist_path = launch_daemons / f"{target.label}.plist"
    _write_launchd_plist(plist_path, target.label)
    loaded = False
    calls = []

    def fake_run(cmd, *, timeout=30):
        nonlocal loaded
        calls.append(cmd)
        if cmd[:2] == ["/bin/launchctl", "print"]:
            return subprocess.CompletedProcess(
                cmd,
                0 if loaded else 1,
                stdout="\tstate = running\n" if loaded else "",
                stderr="",
            )
        if cmd == ["/bin/launchctl", "bootstrap", "system", str(plist_path)]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Operation not permitted")
        if cmd == [
            "/usr/bin/sudo",
            "-n",
            "/bin/launchctl",
            "bootstrap",
            "system",
            str(plist_path),
        ]:
            loaded = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "SYSTEM_LAUNCH_DAEMONS_DIR", launch_daemons)
    monkeypatch.setattr(restart_surfaces, "_run", fake_run)
    monkeypatch.setattr(restart_surfaces.time, "sleep", lambda *_args: None)

    assert restart_surfaces._bootstrap_targets_before_drain(
        (target,),
        uid=503,
        enabled=True,
    ) == []
    assert [
        "/usr/bin/sudo",
        "-n",
        "/bin/launchctl",
        "bootstrap",
        "system",
        str(plist_path),
    ] in calls


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
        lambda _target, service, _before, **_kwargs: (
            graceful_services.append(service) or RestartVerification.RESTARTED,
            None,
        ),
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
    # Neither fallback may vouch for the launchd PID in this test.
    monkeypatch.setattr(restart_surfaces, "_heartbeat_confirms_pid", lambda *_a: False)
    monkeypatch.setattr(restart_surfaces, "_pid_is_alive", lambda _pid: False)
    assert real_gateway_pid(target, before) is None

    monkeypatch.setattr(gateway_status, "get_runtime_status_running_pid", lambda *_a, **_k: 222)
    assert real_gateway_pid(target, before) == 222


def _write_status_and_heartbeat(tmp_path, *, status_pid, heartbeat_pid, heartbeat_age_s):
    """Build a profile-home dir with a stale status file and a heartbeat."""
    from datetime import datetime, timedelta, timezone

    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    status_path = home / "gateway_state.json"
    status_path.write_text(
        json.dumps({"pid": status_pid, "gateway_state": "running"}), encoding="utf-8"
    )
    heartbeat_path = home / "state" / "gateway.heartbeat"
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    updated_at = datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_s)
    heartbeat_path.write_text(
        json.dumps(
            {
                "pid": heartbeat_pid,
                "updated_at": updated_at.isoformat(),
                "monotonic": 1.0,
            }
        ),
        encoding="utf-8",
    )
    return status_path


def test_gateway_pid_trusts_launchd_via_fresh_heartbeat_when_status_stale(
    monkeypatch, tmp_path
):
    """A poisoned/stale gateway_state.json must not block a graceful restart
    while the loop heartbeat proves launchd's PID is the live gateway."""
    from gateway import status as gateway_status
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)
    before = subprocess.CompletedProcess(
        ["launchctl", "print"], 0, stdout="\tpid = 222\n", stderr=""
    )
    status_path = _write_status_and_heartbeat(
        tmp_path, status_pid=145, heartbeat_pid=222, heartbeat_age_s=5
    )
    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(
        restart_surfaces,
        "GATEWAY_STATUS_PATHS",
        {"ai.hermes.gateway": status_path},
    )
    monkeypatch.setattr(
        gateway_status, "get_runtime_status_running_pid", lambda *_a, **_k: None
    )

    assert real_gateway_pid(target, before) == 222
    log = (tmp_path / "restart.log").read_text()
    assert str(status_path) in log
    assert "fresh loop heartbeat" in log


def test_gateway_pid_rejects_future_dated_heartbeat(monkeypatch, tmp_path):
    """A heartbeat dated in the future (clock rollback, forged timestamp) is
    not trusted — only bounded skew around now counts as fresh."""
    from gateway import status as gateway_status
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)
    before = subprocess.CompletedProcess(
        ["launchctl", "print"], 0, stdout="\tpid = 222\n", stderr=""
    )
    status_path = _write_status_and_heartbeat(
        tmp_path, status_pid=145, heartbeat_pid=222, heartbeat_age_s=-3600
    )
    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(
        restart_surfaces,
        "GATEWAY_STATUS_PATHS",
        {"ai.hermes.gateway": status_path},
    )
    monkeypatch.setattr(
        gateway_status, "get_runtime_status_running_pid", lambda *_a, **_k: None
    )
    monkeypatch.setattr(restart_surfaces, "_pid_is_alive", lambda _pid: False)

    assert real_gateway_pid(target, before) is None


def test_gateway_pid_stale_heartbeat_falls_back_to_port_owner(monkeypatch, tmp_path):
    """With a stale heartbeat, a live launchd PID that owns the gateway
    listener port is still trusted (mocked lsof)."""
    from gateway import status as gateway_status
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)
    before = subprocess.CompletedProcess(
        ["launchctl", "print"], 0, stdout="\tpid = 222\n", stderr=""
    )
    status_path = _write_status_and_heartbeat(
        tmp_path, status_pid=145, heartbeat_pid=222, heartbeat_age_s=600
    )
    lsof_calls = []

    def fake_run(cmd, **_kwargs):
        lsof_calls.append(cmd)
        assert "lsof" in cmd[-1]
        assert str(restart_surfaces.GATEWAY_LISTENER_PORT) in cmd[-1]
        return subprocess.CompletedProcess(cmd, 0, stdout="222\n", stderr="")

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(
        restart_surfaces,
        "GATEWAY_STATUS_PATHS",
        {"ai.hermes.gateway": status_path},
    )
    monkeypatch.setattr(
        gateway_status, "get_runtime_status_running_pid", lambda *_a, **_k: None
    )
    monkeypatch.setattr(restart_surfaces, "_pid_is_alive", lambda pid: pid == 222)
    monkeypatch.setattr(restart_surfaces, "_run", fake_run)

    assert real_gateway_pid(target, before) == 222
    assert lsof_calls
    log = (tmp_path / "restart.log").read_text()
    assert f"owns listener port {restart_surfaces.GATEWAY_LISTENER_PORT}" in log


def test_gateway_pid_refuses_when_no_fallback_confirms(monkeypatch, tmp_path):
    """Stale status + stale heartbeat + no listener ownership still refuses."""
    from gateway import status as gateway_status
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)
    before = subprocess.CompletedProcess(
        ["launchctl", "print"], 0, stdout="\tpid = 222\n", stderr=""
    )
    status_path = _write_status_and_heartbeat(
        tmp_path, status_pid=145, heartbeat_pid=999, heartbeat_age_s=5
    )
    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(
        restart_surfaces,
        "GATEWAY_STATUS_PATHS",
        {"ai.hermes.gateway": status_path},
    )
    monkeypatch.setattr(
        gateway_status, "get_runtime_status_running_pid", lambda *_a, **_k: None
    )
    monkeypatch.setattr(restart_surfaces, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(
        restart_surfaces,
        "_run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    assert real_gateway_pid(target, before) is None


def test_graceful_gateway_restart_signals_and_waits_for_launchd_replacement(monkeypatch):
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway", required=True)
    before = subprocess.CompletedProcess(
        ["launchctl", "print"],
        0,
        stdout="\tpid = 100\n",
        stderr="",
    )
    replacement = subprocess.CompletedProcess(
        ["launchctl", "print"],
        0,
        stdout="\tpid = 101\n",
        stderr="",
    )
    signals = []
    gateway_pids = iter((100, 101))
    monkeypatch.setattr(restart_surfaces, "_gateway_pid", lambda *_args: next(gateway_pids))
    monkeypatch.setattr(restart_surfaces, "_launchctl_print", lambda _service: replacement)
    monkeypatch.setattr(restart_surfaces, "_pid_is_alive", lambda pid: False)
    monkeypatch.setattr(restart_surfaces.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    verification, failure = real_graceful_restart_gateway(
        target,
        "user/503/ai.hermes.gateway",
        before,
        timeout=10,
    )

    assert verification is RestartVerification.RESTARTED
    assert failure is None
    assert signals == [(100, restart_surfaces.signal.SIGUSR1)]


def test_graceful_gateway_restart_waits_for_old_pid_exit_and_replacement_status(monkeypatch):
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway-gpt", required=True)
    before = subprocess.CompletedProcess(
        ["launchctl", "print"], 0, stdout="\tpid = 100\n", stderr=""
    )
    replacement = subprocess.CompletedProcess(
        ["launchctl", "print"], 0, stdout="\tpid = 101\n", stderr=""
    )
    old_pid_alive = iter((True, False, False))
    runtime_pids = iter((100, 100, 101))
    monotonic_values = iter((0.0, 0.1, 0.2, 0.3, 0.4))

    monkeypatch.setattr(
        restart_surfaces, "_gateway_pid", lambda *_args: next(runtime_pids)
    )
    monkeypatch.setattr(restart_surfaces, "_launchctl_print", lambda _service: replacement)
    monkeypatch.setattr(
        restart_surfaces,
        "_pid_is_alive",
        lambda pid: next(old_pid_alive) if pid == 100 else True,
    )
    monkeypatch.setattr(restart_surfaces.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(restart_surfaces.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(restart_surfaces.os, "kill", lambda *_args: None)

    verification, failure = real_graceful_restart_gateway(
        target,
        "gui/503/ai.hermes.gateway-gpt",
        before,
        timeout=10,
    )

    assert verification is RestartVerification.RESTARTED
    assert failure is None


def test_graceful_gateway_restart_does_not_trust_status_pid_without_launchd_restart(monkeypatch):
    """A transient status PID must not mark a launchd service restarted.

    Regression: the graceful helper returned success as soon as gateway_state.json
    showed a different live PID, even while launchctl still reported the original
    process. restart_scope then added the service to restarted_services and skipped
    its user/gui twin as "already restarted" although launchd never cycled it.
    """
    from hermes_cli import restart_surfaces

    target = RestartTarget("user/{uid}", "ai.hermes.gateway-poke", required=False)
    before = subprocess.CompletedProcess(
        ["launchctl", "print"],
        0,
        stdout="\tpid = 100\n",
        stderr="",
    )
    monotonic_values = iter((0.0, 0.1, 1.1))
    monkeypatch.setattr(restart_surfaces, "_gateway_pid", lambda *_args: 100)
    monkeypatch.setattr(restart_surfaces, "_read_json", lambda _path: {"pid": 101})
    monkeypatch.setattr(restart_surfaces, "_pid_is_alive", lambda pid: pid == 101)
    monkeypatch.setattr(restart_surfaces, "_launchctl_print", lambda _service: before)
    monkeypatch.setattr(restart_surfaces.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(restart_surfaces.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(restart_surfaces.os, "kill", lambda *_args: None)

    verification, failure = real_graceful_restart_gateway(
        target,
        "user/503/ai.hermes.gateway-poke",
        before,
        timeout=1,
    )

    assert verification is RestartVerification.NOT_RESTARTED
    assert failure == (
        "user/503/ai.hermes.gateway-poke did not complete its graceful "
        "self-restart within 1s"
    )


def test_restart_scope_does_not_skip_twin_after_unverified_restart(monkeypatch, tmp_path):
    """A failed restart proof must not enter the current-run dedup set."""
    from hermes_cli import restart_surfaces

    uid = restart_surfaces.os.getuid()
    user_target = RestartTarget("user/{uid}", "ai.hermes.gateway-poke", required=False)
    gui_target = RestartTarget("gui/{uid}", "ai.hermes.gateway-poke", required=False)
    loaded_service = f"user/{uid}/ai.hermes.gateway-poke"
    before = subprocess.CompletedProcess(
        ["launchctl", "print"],
        0,
        stdout="\tpid = 100\n",
        stderr="",
    )
    graceful_calls = []

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "targets_for_scope", lambda _scope: (user_target, gui_target))
    monkeypatch.setattr(restart_surfaces, "VERIFY_PORTS", {"gateways": ()})
    monkeypatch.setattr(restart_surfaces, "_gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr(
        restart_surfaces,
        "_resolve_loaded_service",
        lambda _requested: (loaded_service, before),
    )
    monkeypatch.setattr(
        restart_surfaces,
        "_graceful_restart_gateway",
        lambda *_args, **_kwargs: (
            graceful_calls.append(_args[1]) or RestartVerification.NOT_RESTARTED,
            "restart was not verified",
        ),
    )
    monkeypatch.setattr(restart_surfaces, "_wait_for_scope_health", lambda *_args, **_kwargs: ([], []))

    assert restart_surfaces.restart_scope("gateways", delay=0) == 1
    assert graceful_calls == [loaded_service, loaded_service]
    assert "already restarted" not in (tmp_path / "restart.log").read_text()


def test_restart_scope_dedupes_twin_when_restart_proof_is_transiently_unverifiable(
    monkeypatch, tmp_path
):
    """Transient launchctl read failures must not signal the fresh twin twice."""
    from hermes_cli import restart_surfaces

    uid = restart_surfaces.os.getuid()
    user_target = RestartTarget("user/{uid}", "ai.hermes.gateway-poke", required=False)
    gui_target = RestartTarget("gui/{uid}", "ai.hermes.gateway-poke", required=False)
    loaded_service = f"user/{uid}/ai.hermes.gateway-poke"
    before = subprocess.CompletedProcess(
        ["launchctl", "print"],
        0,
        stdout="\tpid = 100\n",
        stderr="",
    )
    signals = []

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    monkeypatch.setattr(restart_surfaces, "targets_for_scope", lambda _scope: (user_target, gui_target))
    monkeypatch.setattr(restart_surfaces, "VERIFY_PORTS", {"gateways": ()})
    monkeypatch.setattr(restart_surfaces, "_gateway_busy_details", lambda _targets: [])
    monkeypatch.setattr(
        restart_surfaces,
        "_resolve_loaded_service",
        lambda _requested: (loaded_service, before),
    )
    monkeypatch.setattr(restart_surfaces, "_gateway_pid", lambda *_args: 100)
    monkeypatch.setattr(
        restart_surfaces,
        "_graceful_restart_gateway",
        real_graceful_restart_gateway,
    )
    monkeypatch.setattr(
        restart_surfaces,
        "_launchctl_print",
        lambda _service: subprocess.CompletedProcess(
            ["launchctl", "print"], 1, stdout="", stderr="transient read failure"
        ),
    )
    monkeypatch.setattr(restart_surfaces.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(restart_surfaces.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(restart_surfaces, "_wait_for_scope_health", lambda *_args, **_kwargs: ([], []))

    assert restart_surfaces.restart_scope("gateways", delay=0) == 0
    assert signals == [(100, restart_surfaces.signal.SIGUSR1)]
    log = (tmp_path / "restart.log").read_text()
    assert "verification was inconclusive" in log
    assert "skipping duplicate" in log


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
    assert normalize_scope("restart-webui") == "webui"
    assert normalize_scope("restart_webui") == "webui"
    assert normalize_scope("restart-gateways") == "gateways"
    assert normalize_scope("all") == "hermes"


def test_webui_scope_targets_and_verifies_only_webui():
    assert [target.label for target in targets_for_scope("webui")] == ["ai.hermes.webui"]
    assert verification_ports_for_scope("webui") == (8787,)


def test_system_restart_sudoers_content_is_narrow():
    from hermes_cli import restart_surfaces

    content = restart_surfaces.system_restart_sudoers_content("Kosta")

    assert "Kosta ALL=(root) NOPASSWD: HERMES_RESTART_SURFACES" in content
    assert "/bin/launchctl kickstart -k system/com.kosta.hermes-dashboard-system" in content
    assert (
        "/bin/launchctl bootstrap system "
        "/Library/LaunchDaemons/com.kosta.hermes-dashboard-system.plist"
    ) in content
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


def test_launchctl_print_log_suppresses_secret_values_but_keeps_state(monkeypatch, tmp_path):
    from hermes_cli import restart_surfaces

    monkeypatch.setattr(restart_surfaces, "LOG_PATH", tmp_path / "restart.log")
    launchctl_stdout = """\
service = {
    state = running
    pid = 4321
    environment = {
        DB_PASSWORD => pass-123
        ACCESS_TOKEN => tok-456
        CLIENT_SECRET => secret-789
        OPENAI_API_KEY => key-abc
        AUTH_CREDENTIAL => cred-def
        PATH => /usr/local/bin
    }
}
"""
    launchctl_stderr = "launchctl diagnostic: retryable read error\nSESSION_COOKIE=cookie-ghi"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["/bin/launchctl", "print"],
            0,
            stdout=launchctl_stdout,
            stderr=launchctl_stderr,
        ),
    )

    proc = restart_surfaces._launchctl_print("gui/503/ai.hermes.gateway")

    assert "pass-123" in proc.stdout
    log = (tmp_path / "restart.log").read_text()
    assert "state=running" in log
    assert "pid=4321" in log
    assert "retryable read error" in log
    assert "SESSION_COOKIE=[REDACTED]" in log
    for forbidden in (
        "pass-123",
        "tok-456",
        "secret-789",
        "key-abc",
        "cred-def",
        "cookie-ghi",
        "/usr/local/bin",
    ):
        assert forbidden not in log


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


def _desktop_dashboard_target():
    return RestartTarget(
        "user/{uid}",
        "ai.hermes.desktop-remote-dashboard",
        required=False,
        description="MacBook Hermes Desktop remote dashboard on 9120",
    )


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


def test_desktop_busy_details_reports_active_slash_worker_turns(monkeypatch):
    import io

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    payload = json.dumps({"active_runs": 4, "oldest_run_age_seconds": 83.0}).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: FakeResponse(payload),
    )

    busy = real_desktop_busy_details([_desktop_dashboard_target()])
    assert busy == [
        "ai.hermes.desktop-remote-dashboard: active_runs=4, oldest_run_age_seconds=83.0"
    ]


def test_desktop_busy_details_fails_closed_for_old_or_unreachable_dashboard(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("connection refused")),
    )

    busy = real_desktop_busy_details([_desktop_dashboard_target()])
    assert len(busy) == 1
    assert "health probe unavailable or invalid" in busy[0]


def test_desktop_busy_details_allows_legacy_health_for_upgrade_restart(monkeypatch):
    import io

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    payload = json.dumps({"status": "ok", "marker": "hermes-dashboard-ok"}).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: FakeResponse(payload),
    )

    assert real_desktop_busy_details([_desktop_dashboard_target()]) == []


def test_desktop_busy_details_skips_absent_optional_dashboard(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._desktop_dashboard_is_running",
        lambda: False,
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("absent optional dashboard must not be probed")
        ),
    )

    assert real_desktop_busy_details([_desktop_dashboard_target()]) == []


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
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces._wait_for_scope_health",
        lambda *_args, **_kwargs: ([], []),
    )
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
