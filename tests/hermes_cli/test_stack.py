import argparse
import plistlib
import subprocess

import pytest

from hermes_cli import stack


def write_plist(path, label, args, *, cwd=None, env=None):
    data = {"Label": label, "ProgramArguments": args, "RunAtLoad": True}
    if cwd:
        data["WorkingDirectory"] = cwd
    if env:
        data["EnvironmentVariables"] = env
    with path.open("wb") as fh:
        plistlib.dump(data, fh)


def test_discover_launchagent_services_includes_hermes_web_helpers_and_excludes_model_stack(tmp_path):
    write_plist(
        tmp_path / "ai.hermes.gateway.plist",
        "ai.hermes.gateway",
        ["/bin/hermes", "--profile", "default", "gateway", "run"],
        env={"HERMES_HOME": "/tmp/hermes"},
    )
    write_plist(
        tmp_path / "ai.hermes.comfyui.plist",
        "ai.hermes.comfyui",
        ["/bin/comfyui"],
    )
    write_plist(
        tmp_path / "com.kosta.hermes-voice-bridge.plist",
        "com.kosta.hermes-voice-bridge",
        ["/bin/python", "voice.py"],
        cwd="/tmp/voice",
    )

    write_plist(
        tmp_path / "com.remodex.bridge.plist",
        "com.remodex.bridge",
        ["/bin/remodex", "run-service"],
    )

    write_plist(
        tmp_path / "com.kosta.claude-hermes-telegram.plist",
        "com.kosta.claude-hermes-telegram",
        ["/bin/claude-watch", "telegram"],
    )

    services = {svc.name: svc for svc in stack.discover_launchagent_services(tmp_path)}

    assert "gateway" in services
    assert services["gateway"].health.url == "http://127.0.0.1:8642/health"
    assert services["gateway"].env == {"HERMES_HOME": "/tmp/hermes"}
    assert "hermes-voice-bridge" in services
    assert services["hermes-voice-bridge"].cwd == "/tmp/voice"
    assert all("comfyui" not in name for name in services)
    assert all("remodex" not in name for name in services)
    assert all("claude-hermes" not in name for name in services)


def test_load_manifest_rejects_external_services_with_restart_policy(tmp_path):
    manifest = tmp_path / "stack.yaml"
    manifest.write_text(
        """
services:
  gamingpc-model-api:
    cmd: ["ssh", "gamingpc", "model-server"]
    external: true
    restart: on-failure
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="external service gamingpc-model-api must use restart: never"):
        stack.load_manifest(manifest)


def test_load_manifest_rejects_dependency_cycles(tmp_path):
    manifest = tmp_path / "stack.yaml"
    manifest.write_text(
        """
services:
  a:
    cmd: ["a"]
    depends_on: ["b"]
  b:
    cmd: ["b"]
    depends_on: ["a"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dependency cycle: a -> b -> a"):
        stack.load_manifest(manifest)


def test_load_manifest_requires_explicit_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(FileNotFoundError, match="stack manifest not found"):
        stack.load_manifest()


def test_render_manifest_round_trips_local_service(tmp_path):
    original = {
        "gateway-gpt": stack.ServiceSpec(
            name="gateway-gpt",
            cmd=("hermes", "--profile", "gpt", "gateway", "run"),
            health=stack.HealthCheck(url="http://127.0.0.1:8643/health"),
            critical=True,
        )
    }
    manifest = tmp_path / "stack.yaml"
    manifest.write_text(stack.render_manifest(original), encoding="utf-8")

    loaded = stack.load_manifest(manifest)

    assert loaded["gateway-gpt"].cmd == ("hermes", "--profile", "gpt", "gateway", "run")
    assert loaded["gateway-gpt"].health.url == "http://127.0.0.1:8643/health"
    assert loaded["gateway-gpt"].critical is True


def test_supervisor_refuses_to_start_external_service():
    supervisor = stack.StackSupervisor(
        {
            "rtx": stack.ServiceSpec(
                name="rtx",
                cmd=("ssh", "gamingpc", "server"),
                restart="never",
                external=True,
            )
        }
    )

    assert supervisor.start("rtx") == {"ok": False, "error": "external service is health-check only"}


def test_supervisor_detects_blocking_port_owner(monkeypatch):
    monkeypatch.setattr(stack, "_port_owner", lambda port: 1234)
    supervisor = stack.StackSupervisor(
        {
            "api": stack.ServiceSpec(
                name="api",
                cmd=("python", "server.py"),
                health=stack.HealthCheck(port=8642),
            )
        }
    )

    result = supervisor.start("api")

    assert result == {"ok": False, "error": "port owned by pid 1234", "pid": 1234}


def test_supervisor_starts_dependencies_before_service(monkeypatch, tmp_path):
    started = []

    class FakeProcess:
        def __init__(self, cmd, **kwargs):
            started.append(tuple(cmd))
            self.pid = len(started) + 100
            self.returncode = None

        def poll(self):
            return None

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    supervisor = stack.StackSupervisor(
        {
            "backend": stack.ServiceSpec(name="backend", cmd=("backend",)),
            "frontend": stack.ServiceSpec(name="frontend", cmd=("frontend",), depends_on=("backend",)),
        }
    )

    result = supervisor.start("frontend")

    assert result["ok"] is True
    assert started == [("backend",), ("frontend",)]


def test_supervisor_allows_healthy_external_dependency(monkeypatch, tmp_path):
    started = []

    class FakeProcess:
        def __init__(self, cmd, **kwargs):
            started.append(tuple(cmd))
            self.pid = 200
            self.returncode = None

        def poll(self):
            return None

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(stack, "health_state", lambda spec, process=None: "healthy" if spec.external else "running")
    supervisor = stack.StackSupervisor(
        {
            "rtx": stack.ServiceSpec(name="rtx", cmd=("ssh", "gamingpc"), restart="never", external=True),
            "gateway": stack.ServiceSpec(name="gateway", cmd=("gateway",), depends_on=("rtx",)),
        }
    )

    result = supervisor.start("gateway")

    assert result["ok"] is True
    assert started == [("gateway",)]


def test_stack_parser_registers_builtin_command():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    stack.register_stack_parser(subparsers)

    args = parser.parse_args(["stack", "restart", "gateway-gpt"])

    assert args.command == "stack"
    assert args.stack_command == "restart"
    assert args.service == "gateway-gpt"
    assert args.func is stack.cmd_stack
