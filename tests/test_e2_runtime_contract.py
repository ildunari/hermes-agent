"""Executable E2 runtime contracts; no implementation-source assertions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import hermes_constants
from hermes_constants import (
    agent_browser_runnable,
    find_node_executable,
    node_tool_runnable,
    node_version_supported,
    with_hermes_node_path,
)
from hermes_cli import dep_ensure
from hermes_cli.doctor import browser_runtime_status


ROOT = Path(__file__).resolve().parent.parent


def _assert_exact_installer_result(
    *, env: dict[str, str], prefix: Path, executable: Path
) -> None:
    """Execute the installed CLI and query npm's resolved package metadata."""
    version = subprocess.run(
        [str(executable), "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert version.stdout.strip() == "agent-browser 0.32.0"

    installed = subprocess.run(
        [
            "npm",
            "list",
            "-g",
            "--prefix",
            str(prefix),
            "--depth=0",
            "--json",
            "agent-browser",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(installed.stdout)["dependencies"]["agent-browser"]["version"] == "0.32.0"


def _executable(path: Path, output: str, *, exit_code: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {exit_code}\n")
    path.chmod(0o755)
    return path


def _node24_env() -> dict[str, str]:
    candidates = [
        Path(os.environ.get("HERMES_TEST_NODE24_BIN", "/nonexistent")),
        Path("/opt/homebrew/opt/node@24/bin"),
        Path(shutil.which("node") or "/nonexistent").parent,
    ]
    for bin_dir in candidates:
        node = bin_dir / ("node.exe" if os.name == "nt" else "node")
        npm = bin_dir / ("npm.cmd" if os.name == "nt" else "npm")
        if node_version_supported(str(node)) and npm.is_file():
            env = os.environ.copy()
            env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
            env["npm_config_engine_strict"] = "true"
            return env
    pytest.fail("Node >=24 + npm are required for the engine-strict contract")


def test_node_floor_predicate_executes_candidate(tmp_path: Path) -> None:
    assert node_version_supported(str(_executable(tmp_path / "node22", "v22.23.1"))) is False
    assert node_version_supported(str(_executable(tmp_path / "node24", "v24.0.0"))) is True
    assert node_version_supported(str(_executable(tmp_path / "node26", "v26.1.0"))) is True
    assert node_version_supported(str(_executable(tmp_path / "bad", "not-a-version"))) is False


def test_managed_node22_is_not_prepended_and_heals_before_browser_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes"
    managed = home / "node" / "bin"
    node = _executable(managed / "node", "v22.23.1")
    npm = _executable(managed / "npm", "10.9.4")
    browser = _executable(managed / "agent-browser", "agent-browser 0.32.0")
    system = tmp_path / "system"
    _executable(system / "node", "v24.18.0")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PATH", str(system))
    monkeypatch.setattr(hermes_constants, "_managed_node_heal_attempted", False)

    assert node_tool_runnable(str(npm)) is False
    assert str(managed) not in with_hermes_node_path()["PATH"].split(os.pathsep)

    healed = False

    def heal() -> bool:
        nonlocal healed
        healed = True
        _executable(node, "v24.18.0")
        return True

    monkeypatch.setattr(hermes_constants, "heal_hermes_managed_node", heal)
    assert agent_browser_runnable(str(browser)) is True
    assert healed is True
    assert find_node_executable("node") == str(node)
    assert with_hermes_node_path()["PATH"].split(os.pathsep)[0] == str(managed)


def test_browser_dependency_rejects_exact_cli_under_node22(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    _executable(bin_dir / "node", "v22.23.1")
    _executable(bin_dir / "agent-browser", "agent-browser 0.32.0")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(dep_ensure.shutil, "which", shutil.which)
    assert dep_ensure._DEP_CHECKS["browser"]() is False


@pytest.mark.parametrize("version", ["0.27.0", "0.32.1", "99.0.0"])
def test_doctor_rejects_nonexact_local_agent_browser(
    version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "runtime"
    node = _executable(bin_dir / "node", "v24.18.0")
    local = _executable(
        tmp_path / "project" / "node_modules" / ".bin" / "agent-browser",
        f"agent-browser {version}",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PATH", str(bin_dir))
    node_ok, candidate, browser_ok = browser_runtime_status(
        tmp_path / "project", node_path=str(node), path_agent_browser=str(local)
    )
    assert node_ok is True
    assert candidate == str(local)
    assert browser_ok is False


def test_doctor_accepts_only_exact_local_agent_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "runtime"
    node = _executable(bin_dir / "node", "v24.18.0")
    local = _executable(
        tmp_path / "project" / "node_modules" / ".bin" / "agent-browser",
        "agent-browser 0.32.0",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PATH", str(bin_dir))
    assert browser_runtime_status(
        tmp_path / "project", node_path=str(node), path_agent_browser=str(local)
    ) == (True, str(local), True)


@pytest.mark.parametrize("version", ["0.27.0", "0.32.1", "99.0.0"])
def test_setup_never_executes_nonexact_local_cli_for_chromium(
    version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_cli import tools_config

    project = tmp_path / "project"
    bin_dir = tmp_path / "runtime"
    _executable(bin_dir / "node", "v24.18.0")
    _executable(bin_dir / "npm", "11.16.0")
    stale = _executable(
        project / "node_modules" / ".bin" / "agent-browser",
        f"agent-browser {version}",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(tools_config, "PROJECT_ROOT", project)
    calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        if len(command) > 1 and command[1] == "--version":
            return real_run(command, **_kwargs)
        calls.append([str(part) for part in command])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tools_config._run_post_setup("agent_browser")
    assert calls and calls[0][0] == str(bin_dir / "npm")
    assert [str(stale), "install", "--with-deps"] not in calls


def test_root_engine_strict_install_and_exact_dependency() -> None:
    env = _node24_env()
    metadata = subprocess.run(
        ["npm", "pkg", "get", "engines.node", "dependencies.agent-browser"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    values = json.loads(metadata.stdout)
    assert values == {"engines.node": ">=24.0.0", "dependencies.agent-browser": "0.32.0"}
    result = subprocess.run(
        ["npm", "ci", "--ignore-scripts", "--dry-run", "--workspaces=false"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EBADENGINE" not in result.stdout + result.stderr


def test_termux_package_runtime_is_executably_verified(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _executable(bin_dir / "pkg", "")
    node = _executable(bin_dir / "node", "v22.23.1")
    env = os.environ.copy()
    env.update(
        {
            "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
            "TERMUX_VERSION": "test",
            "PREFIX": str(tmp_path / "com.termux" / "files" / "usr"),
            "HERMES_HOME": str(tmp_path / "home"),
        }
    )
    command = f'. "{ROOT / "scripts/lib/node-bootstrap.sh"}"; _nb_try_termux_pkg'
    old = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
    assert old.returncode != 0
    _executable(node, "v24.18.0")
    modern = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
    assert modern.returncode == 0, modern.stdout + modern.stderr


def test_shipping_installer_termux_path_fails_on_pkg_node22(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _executable(bin_dir / "node", "v22.23.1")
    _executable(bin_dir / "pkg", "")
    uname = bin_dir / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        "case \"${1:-}\" in -s) echo Linux ;; -m) echo aarch64 ;; *) echo Linux ;; esac\n"
    )
    uname.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
            "TERMUX_VERSION": "test",
            "PREFIX": str(tmp_path / "com.termux" / "files" / "usr"),
            "HOME": str(tmp_path / "home"),
            "HERMES_HOME": str(tmp_path / "home" / ".hermes"),
        }
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install.sh"), "--ensure", "node"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "unsupported Node.js v22.23.1" in result.stdout + result.stderr


def test_nix_container_migrates_stale_node22_path_to_exact_node24(
    tmp_path: Path,
) -> None:
    stale = tmp_path / "apt" / "bin"
    immutable = tmp_path / "nix-store-node" / "bin"
    _executable(stale / "node", "v22.23.1")
    _executable(immutable / "node", "v24.18.0")
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": os.pathsep.join([str(stale), "/usr/bin", "/bin"]),
            "HOME": str(home),
            "TARGET_HOME": str(home),
            "HERMES_CONTAINER_NODE_BIN": str(immutable),
            "HERMES_CONTAINER_NODE_VERSION": "24.18.0",
        }
    )
    contract = ROOT / "nix" / "container-node-runtime.sh"
    command = f'. "{contract}"; hermes_activate_immutable_node; node --version; command -v node'
    result = subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["v24.18.0", str(immutable / "node")]


@pytest.mark.skipif(shutil.which("nix") is None, reason="Nix evaluator unavailable")
def test_nix_lock_resolves_exact_node_24_18() -> None:
    expression = (
        "(builtins.getFlake (toString ./.)).inputs.nixpkgs."
        "legacyPackages.x86_64-linux.nodejs_24.version"
    )
    result = subprocess.run(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "eval",
            "--impure",
            "--raw",
            "--expr",
            expression,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "24.18.0"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows execution contract")
def test_windows_installer_parses_with_windows_powershell() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell
    script = ROOT / "scripts" / "install.ps1"
    command = (
        f"$e=$null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}',[ref]$null,[ref]$e)|Out-Null; if($e.Count){{exit 1}}"
    )
    subprocess.run([powershell, "-NoProfile", "-Command", command], check=True)


@pytest.mark.skipif(
    sys.platform != "win32"
    or os.environ.get("HERMES_RUN_REAL_NODE_INSTALLER_TEST") != "1",
    reason="Windows + opt-in networked installer contract",
)
def test_real_isolated_windows_browser_installer_resolves_exact_package(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell
    env = _node24_env()
    env["npm_config_engine_strict"] = "true"
    home = tmp_path / "hermes-home"
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "install.ps1"),
            "-Ensure",
            "browser",
            "-HermesHome",
            str(home),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_exact_installer_result(
        env=env,
        prefix=home / "node",
        executable=home / "node" / "agent-browser.cmd",
    )


@pytest.mark.skipif(
    os.environ.get("HERMES_RUN_REAL_NODE_INSTALLER_TEST") != "1",
    reason="set HERMES_RUN_REAL_NODE_INSTALLER_TEST=1 for networked installer proof",
)
def test_real_isolated_posix_browser_installer_resolves_exact_package_on_node24(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX installer regression")
    env = _node24_env()
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.is_file():
        env["AGENT_BROWSER_EXECUTABLE_PATH"] = str(chrome)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/install.sh"),
            "--ensure",
            "browser",
            "--hermes-home",
            env["HERMES_HOME"],
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = Path(env["HERMES_HOME"]) / "node"
    _assert_exact_installer_result(
        env=env,
        prefix=prefix,
        executable=prefix / "bin" / "agent-browser",
    )
