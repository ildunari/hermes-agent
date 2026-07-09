"""Hermes stack supervisor.

One launchd job can run ``hermes stack run`` while this module supervises the
Mac-local Hermes gateways and helper web/app processes as individually managed
children. Remote/model services are intentionally health-checked only when a
manifest marks them external; this supervisor must not own GamingPC services.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil is a core dep, this is a packaging guard.
    psutil = None  # type: ignore

STACK_LABEL = "ai.hermes.stack"
DEFAULT_SOCKET_NAME = "stack.sock"
DEFAULT_MANIFEST_NAME = "stack.yaml"
DEFAULT_STATE_DIR = "run/stack"
DEFAULT_LOG_DIR = "logs/stack"

DEFAULT_HEALTH_BY_LABEL = {
    "ai.hermes.gateway": "http://127.0.0.1:8642/health",
    "ai.hermes.gateway-gpt": "http://127.0.0.1:8643/health",
    "ai.hermes.gateway-browser-agent": "http://127.0.0.1:8644/health",
    "ai.hermes.webui": "http://127.0.0.1:18643/health",
    "ai.hermes.workspace-proxy": "http://127.0.0.1:3192/health",
    "com.kosta.hermes-gpt-api-proxy": "http://127.0.0.1:8645/health",
    "com.kosta.hermes-voice-bridge": "http://127.0.0.1:8776/health",
}

# Local Hermes gateway / web-app / helper surface only.  Model stacks such as
# ComfyUI, GamingPC/RTX APIs, or generic watchdogs are deliberately excluded.
MANAGED_LABEL_PREFIXES = (
    "ai.hermes.",
    "com.kosta.hermes-",
    "com.kosta.clawket-hermes-",
)
MANAGED_LABEL_MARKERS = (
    "gateway",
    "webui",
    "workspace",
    "proxy",
    "bridge",
    "claude-hermes",
    "clawket-hermes",
)
EXCLUDED_LABEL_MARKERS = (
    "comfyui",
    "codex-subtask-supervisor",
    "watchdog",
)


@dataclass(frozen=True)
class HealthCheck:
    url: str | None = None
    port: int | None = None
    timeout: float = 2.0


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    cmd: tuple[str, ...]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    health: HealthCheck = field(default_factory=HealthCheck)
    depends_on: tuple[str, ...] = ()
    restart: str = "on-failure"
    critical: bool = False
    external: bool = False
    adopt_orphans: bool = False
    stop_timeout: float = 10.0
    start_grace: float = 10.0


@dataclass
class ServiceRuntime:
    spec: ServiceSpec
    process: subprocess.Popen | None = None
    restart_count: int = 0
    last_start: float = 0.0
    last_exit: int | None = None
    backoff_until: float = 0.0
    wants_running: bool = False
    log_handle: Any | None = None


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def default_manifest_path() -> Path:
    return hermes_home() / DEFAULT_MANIFEST_NAME


def state_dir() -> Path:
    return hermes_home() / DEFAULT_STATE_DIR


def socket_path() -> Path:
    return state_dir() / DEFAULT_SOCKET_NAME


def log_dir() -> Path:
    return hermes_home() / DEFAULT_LOG_DIR


def _service_name_from_label(label: str) -> str:
    name = label
    for prefix in ("ai.hermes.", "com.kosta."):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.replace("_", "-")


def _is_managed_launchd_label(label: str) -> bool:
    lowered = label.lower()
    if not any(lowered.startswith(prefix) for prefix in MANAGED_LABEL_PREFIXES):
        return False
    if any(marker in lowered for marker in EXCLUDED_LABEL_MARKERS):
        return False
    return any(marker in lowered for marker in MANAGED_LABEL_MARKERS)


def _load_launchagent_service(path: Path) -> ServiceSpec | None:
    try:
        with path.open("rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        return None
    label = str(data.get("Label") or "")
    args = data.get("ProgramArguments")
    if not label or not isinstance(args, list) or not _is_managed_launchd_label(label):
        return None
    cmd = tuple(str(part) for part in args if str(part))
    if not cmd:
        return None
    env = data.get("EnvironmentVariables") if isinstance(data.get("EnvironmentVariables"), dict) else {}
    working_dir = data.get("WorkingDirectory")
    health = HealthCheck(url=DEFAULT_HEALTH_BY_LABEL.get(label))
    return ServiceSpec(
        name=_service_name_from_label(label),
        cmd=cmd,
        cwd=str(working_dir) if working_dir else None,
        env={str(k): str(v) for k, v in env.items()},
        health=health,
        restart="on-failure",
        critical="gateway" in label,
    )


def discover_launchagent_services(launchagents_dir: Path | None = None) -> list[ServiceSpec]:
    base = launchagents_dir or Path.home() / "Library" / "LaunchAgents"
    services: list[ServiceSpec] = []
    if not base.exists():
        return services
    for plist_path in sorted(base.glob("*.plist")):
        service = _load_launchagent_service(plist_path)
        if service is not None:
            services.append(service)
    return services


def load_manifest(path: Path | None = None) -> dict[str, ServiceSpec]:
    manifest_path = path or default_manifest_path()
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"stack manifest not found: {manifest_path}. "
            "Run `hermes stack manifest --from-launchd > ~/.hermes/stack.yaml` "
            "and inspect it before running the supervisor."
        )

    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    service_data = raw.get("services") if isinstance(raw, dict) else None
    if not isinstance(service_data, dict):
        raise ValueError(f"{manifest_path} must contain a mapping at services")

    services: dict[str, ServiceSpec] = {}
    for name, item in service_data.items():
        if not isinstance(item, dict):
            raise ValueError(f"services.{name} must be a mapping")
        cmd = item.get("cmd")
        if not isinstance(cmd, list) or not all(isinstance(part, str) and part for part in cmd):
            raise ValueError(f"services.{name}.cmd must be a non-empty string list")
        raw_health = item.get("health")
        health_data: dict[str, Any] = raw_health if isinstance(raw_health, dict) else {}
        raw_port = health_data.get("port")
        health = HealthCheck(
            url=health_data.get("url") if isinstance(health_data.get("url"), str) else None,
            port=int(raw_port) if raw_port is not None else None,
            timeout=float(health_data.get("timeout", 2.0)),
        )
        depends = item.get("depends_on") or []
        if not isinstance(depends, list) or not all(isinstance(dep, str) for dep in depends):
            raise ValueError(f"services.{name}.depends_on must be a string list")
        env = item.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError(f"services.{name}.env must be a mapping")
        services[str(name)] = ServiceSpec(
            name=str(name),
            cmd=tuple(cmd),
            cwd=item.get("cwd") if isinstance(item.get("cwd"), str) else None,
            env={str(k): str(v) for k, v in env.items()},
            health=health,
            depends_on=tuple(depends),
            restart=str(item.get("restart", "on-failure")),
            critical=bool(item.get("critical", False)),
            external=bool(item.get("external", False)),
            adopt_orphans=False,
            stop_timeout=float(item.get("stop_timeout", 10.0)),
            start_grace=float(item.get("start_grace", 10.0)),
        )
    _validate_manifest(services)
    return services


def _validate_manifest(services: dict[str, ServiceSpec]) -> None:
    for service in services.values():
        for dep in service.depends_on:
            if dep not in services:
                raise ValueError(f"{service.name} depends on unknown service {dep}")
        if service.external and service.restart != "never":
            raise ValueError(f"external service {service.name} must use restart: never")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        if name in visited:
            return
        if name in visiting:
            cycle = " -> ".join(path + [name])
            raise ValueError(f"dependency cycle: {cycle}")
        visiting.add(name)
        for dep in services[name].depends_on:
            visit(dep, path + [name])
        visiting.remove(name)
        visited.add(name)

    for name in services:
        visit(name, [])


def render_manifest(services: dict[str, ServiceSpec]) -> str:
    data: dict[str, Any] = {"services": {}}
    for name in sorted(services):
        spec = services[name]
        item: dict[str, Any] = {
            "cmd": list(spec.cmd),
            "restart": spec.restart,
            "critical": spec.critical,
        }
        if spec.cwd:
            item["cwd"] = spec.cwd
        if spec.env:
            item["env"] = spec.env
        if spec.depends_on:
            item["depends_on"] = list(spec.depends_on)
        health: dict[str, Any] = {}
        if spec.health.url:
            health["url"] = spec.health.url
        if spec.health.port:
            health["port"] = spec.health.port
        if spec.health.timeout != 2.0:
            health["timeout"] = spec.health.timeout
        if health:
            item["health"] = health
        if spec.external:
            item["external"] = True
        if spec.stop_timeout != 10.0:
            item["stop_timeout"] = spec.stop_timeout
        if spec.start_grace != 10.0:
            item["start_grace"] = spec.start_grace
        data["services"][name] = item
    return yaml.safe_dump(data, sort_keys=False)


def check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_http(url: str, timeout: float = 2.0) -> bool:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local health URLs from manifest.
            return 200 <= int(response.status) < 500
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def health_state(spec: ServiceSpec, process: subprocess.Popen | None = None) -> str:
    if spec.health.url:
        return "healthy" if check_http(spec.health.url, spec.health.timeout) else "unhealthy"
    if spec.health.port:
        return "healthy" if check_port("127.0.0.1", spec.health.port, spec.health.timeout) else "unhealthy"
    if spec.external:
        return "unknown"
    if process is not None and process.poll() is None:
        return "running"
    return "unknown"


def _port_owner(port: int) -> int | None:
    if psutil is None:
        return None
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port and conn.pid:
                return int(conn.pid)
    except Exception:
        return None
    return None


def _pid_command(pid: int) -> str:
    if psutil is None:
        return ""
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except Exception:
        return ""


def diagnose_services(services: dict[str, ServiceSpec]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in services.values():
        port_owner = _port_owner(spec.health.port) if spec.health.port else None
        rows.append(
            {
                "name": spec.name,
                "external": spec.external,
                "health": health_state(spec),
                "port": spec.health.port,
                "port_owner": port_owner,
                "port_owner_cmd": _pid_command(port_owner) if port_owner else "",
                "health_url": spec.health.url,
            }
        )
    return rows


class StackSupervisor:
    def __init__(self, services: dict[str, ServiceSpec]) -> None:
        self.services = services
        self.runtime = {name: ServiceRuntime(spec) for name, spec in services.items()}
        self.stopping = False

    def start(self, name: str) -> dict[str, Any]:
        runtime = self._runtime(name)
        spec = runtime.spec
        runtime.wants_running = True
        if spec.external:
            return {"ok": False, "error": "external service is health-check only"}
        if runtime.process is not None and runtime.process.poll() is None:
            return {"ok": True, "status": "already-running", "pid": runtime.process.pid}
        for dep in spec.depends_on:
            dep_runtime = self._runtime(dep)
            if dep_runtime.spec.external:
                dep_health = health_state(dep_runtime.spec)
                if dep_health != "healthy":
                    return {"ok": False, "error": f"external dependency {dep} is {dep_health}"}
                continue
            dep_result = self.start(dep)
            if not dep_result.get("ok"):
                return {"ok": False, "error": f"dependency {dep} failed: {dep_result.get('error')}"}
            if not self._wait_until_ready(dep_runtime.spec, dep_runtime.process):
                return {"ok": False, "error": f"dependency {dep} did not become ready"}
        conflict = self._blocking_port_owner(spec)
        if conflict:
            return {"ok": False, "error": f"port owned by pid {conflict}", "pid": conflict}
        log_dir().mkdir(parents=True, exist_ok=True)
        log_path = log_dir() / f"{name}.log"
        runtime.log_handle = log_path.open("ab", buffering=0)
        env = os.environ.copy()
        env.update(spec.env)
        runtime.process = subprocess.Popen(
            list(spec.cmd),
            cwd=spec.cwd or None,
            env=env,
            stdout=runtime.log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        runtime.last_start = time.time()
        if spec.health.url or spec.health.port:
            ready = self._wait_until_ready(spec, runtime.process)
            return {
                "ok": True,
                "status": "started" if ready else "started-but-unhealthy",
                "pid": runtime.process.pid,
            }
        return {"ok": True, "status": "started", "pid": runtime.process.pid}

    def stop(self, name: str) -> dict[str, Any]:
        runtime = self._runtime(name)
        runtime.wants_running = False
        proc = runtime.process
        if proc is None or proc.poll() is not None:
            return {"ok": True, "status": "already-stopped"}
        _terminate_process_group(proc, runtime.spec.stop_timeout)
        runtime.last_exit = proc.poll()
        self._close_log(runtime)
        return {"ok": True, "status": "stopped", "exit": runtime.last_exit}

    def restart(self, name: str) -> dict[str, Any]:
        stopped = self.stop(name)
        if not stopped.get("ok"):
            return stopped
        self._runtime(name).wants_running = True
        return self.start(name)

    def status(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for name, runtime in sorted(self.runtime.items()):
            proc = runtime.process
            pid = proc.pid if proc is not None and proc.poll() is None else None
            rows.append(
                {
                    "name": name,
                    "pid": pid,
                    "state": "running" if pid else "stopped",
                    "health": health_state(runtime.spec, proc),
                    "critical": runtime.spec.critical,
                    "external": runtime.spec.external,
                    "restart_count": runtime.restart_count,
                    "last_exit": runtime.last_exit,
                }
            )
        return {"ok": True, "services": rows}

    def tick(self) -> None:
        now = time.time()
        for runtime in self.runtime.values():
            spec = runtime.spec
            proc = runtime.process
            if spec.external:
                continue
            if proc is None:
                if runtime.wants_running and spec.restart != "never" and now >= runtime.backoff_until:
                    runtime.backoff_until = now + min(300.0, 2.0 ** min(runtime.restart_count, 8))
                    self.start(spec.name)
                continue
            if proc.poll() is None:
                continue
            runtime.last_exit = proc.returncode
            self._close_log(runtime)
            runtime.process = None
            if self.stopping or spec.restart == "never":
                continue
            if spec.restart == "always" or (spec.restart == "on-failure" and proc.returncode != 0):
                if now < runtime.backoff_until:
                    continue
                if runtime.last_start and now - runtime.last_start > 120:
                    runtime.restart_count = 0
                runtime.restart_count += 1
                runtime.backoff_until = now + min(300.0, 2.0 ** min(runtime.restart_count, 8))
                self.start(spec.name)

    def serve_forever(self) -> None:
        state_dir().mkdir(parents=True, exist_ok=True)
        sock_path = socket_path()
        if sock_path.exists():
            probe = send_control("status")
            if probe.get("ok"):
                raise RuntimeError(f"stack supervisor already running at {sock_path}")
            sock_path.unlink()
        previous_handlers: dict[int, Any] = {}

        def request_stop(signum: int, _frame: Any) -> None:
            self.stopping = True

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o077)
        try:
            server.bind(str(sock_path))
        finally:
            os.umask(old_umask)
        os.chmod(sock_path, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        try:
            while not self.stopping:
                self.tick()
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(2.0)
                    response = self._handle_client(client)
                    client.sendall((json.dumps(response) + "\n").encode("utf-8"))
        finally:
            self.stopping = True
            for name in list(self.runtime):
                self.stop(name)
            server.close()
            try:
                sock_path.unlink()
            except FileNotFoundError:
                pass
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

    def _handle_client(self, client: socket.socket) -> dict[str, Any]:
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                return {"ok": False, "error": "control request timed out"}
            if not chunk:
                break
            total += len(chunk)
            if total > 65536:
                return {"ok": False, "error": "control request too large"}
            chunks.append(chunk)
        raw = b"".join(chunks).decode("utf-8").strip()
        try:
            request = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid json: {exc}"}
        command = request.get("command")
        name = request.get("service")
        try:
            if command == "status":
                return self.status()
            if command == "start" and isinstance(name, str):
                return self.start(name)
            if command == "stop" and isinstance(name, str):
                return self.stop(name)
            if command == "restart" and isinstance(name, str):
                return self.restart(name)
            if command == "shutdown":
                self.stopping = True
                return {"ok": True}
        except Exception as exc:  # Keep the control socket alive after per-service failures.
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "unknown command"}

    def _runtime(self, name: str) -> ServiceRuntime:
        if name not in self.runtime:
            raise KeyError(f"unknown service {name}")
        return self.runtime[name]

    @staticmethod
    def _wait_until_ready(spec: ServiceSpec, process: subprocess.Popen | None) -> bool:
        deadline = time.monotonic() + max(spec.start_grace, 0.0)
        while True:
            if process is not None and process.poll() is not None:
                return False
            state = health_state(spec, process)
            if state in {"healthy", "running", "unknown"} and not (spec.health.url or spec.health.port):
                return True
            if state == "healthy":
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _blocking_port_owner(self, spec: ServiceSpec) -> int | None:
        port = spec.health.port
        if not port:
            return None
        owner = _port_owner(port)
        if not owner:
            return None
        return owner

    @staticmethod
    def _close_log(runtime: ServiceRuntime) -> None:
        if runtime.log_handle is not None:
            runtime.log_handle.close()
            runtime.log_handle = None


def _terminate_process_group(proc: subprocess.Popen, timeout: float) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        proc.kill()
    proc.wait(timeout=5)


def send_control(command: str, service: str | None = None) -> dict[str, Any]:
    path = socket_path()
    if not path.exists():
        return {"ok": False, "error": f"stack supervisor socket not found: {path}"}
    request = {"command": command}
    if service:
        request["service"] = service
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            client.sendall(json.dumps(request).encode("utf-8"))
            client.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks).decode("utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"stack supervisor unavailable: {exc}"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid supervisor response: {exc}"}


def generate_launchd_plist() -> str:
    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        program_args = [hermes_bin, "stack", "run"]
    else:
        program_args = [sys.executable, "-m", "hermes_cli.main", "stack", "run"]
    data = {
        "Label": STACK_LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(hermes_home() / "logs" / "stack-supervisor.log"),
        "StandardErrorPath": str(hermes_home() / "logs" / "stack-supervisor.err.log"),
        "EnvironmentVariables": {
            "HERMES_HOME": str(hermes_home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }
    import io

    buf = io.BytesIO()
    plistlib.dump(data, buf, sort_keys=False)
    return buf.getvalue().decode("utf-8")


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def cmd_stack(args: argparse.Namespace) -> None:
    command = args.stack_command or "status"
    manifest_path = Path(args.manifest) if getattr(args, "manifest", None) else None
    if command == "plist":
        print(generate_launchd_plist(), end="")
        return
    if command == "manifest" and getattr(args, "from_launchd", False):
        discovered = {service.name: service for service in discover_launchagent_services()}
        print(render_manifest(discovered), end="")
        return
    if command in {"status", "health"}:
        response = send_control("status")
        if not response.get("ok"):
            try:
                services = load_manifest(manifest_path)
            except FileNotFoundError as exc:
                response = {"ok": False, "supervisor": "not-running", "error": str(exc)}
            else:
                response = {"ok": True, "supervisor": "not-running", "services": diagnose_services(services)}
        _print_json(response)
        return

    services = load_manifest(manifest_path)
    if command == "run":
        supervisor = StackSupervisor(services)
        supervisor.serve_forever()
        return
    if command == "manifest":
        print(render_manifest(services), end="")
        return
    if command == "doctor":
        _print_json({"ok": True, "services": diagnose_services(services)})
        return
    if command in {"start", "stop", "restart"}:
        response = send_control(command, args.service)
        _print_json(response)
        if not response.get("ok"):
            raise SystemExit(1)
        return
    raise SystemExit(f"unknown stack command: {command}")


def register_stack_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "stack",
        help="Supervise Mac-local Hermes gateways and helper web/app services",
        description=(
            "Run or inspect the Hermes stack supervisor. This manages only "
            "Mac-local Hermes gateways, web apps, proxies, and bridges; remote "
            "GamingPC/model services are health-check only."
        ),
    )
    parser.add_argument("--manifest", help="Path to stack.yaml (default: ~/.hermes/stack.yaml)")
    stack_subparsers = parser.add_subparsers(dest="stack_command")
    stack_subparsers.add_parser("run", help="Run the long-lived supervisor in the foreground")
    stack_subparsers.add_parser("status", help="Show supervisor/service status")
    stack_subparsers.add_parser("health", help="Alias for status with health checks")
    stack_subparsers.add_parser("doctor", help="Inspect configured services without controlling them")
    manifest_parser = stack_subparsers.add_parser("manifest", help="Print the effective manifest")
    manifest_parser.add_argument(
        "--from-launchd",
        action="store_true",
        help="Discover eligible Mac-local Hermes LaunchAgents and print a candidate manifest",
    )
    stack_subparsers.add_parser("plist", help="Print the launchd plist for ai.hermes.stack")
    for command in ("start", "stop", "restart"):
        child = stack_subparsers.add_parser(command, help=f"{command.title()} one managed service")
        child.add_argument("service")
    parser.set_defaults(func=cmd_stack)
