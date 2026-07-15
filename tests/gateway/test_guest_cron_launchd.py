from __future__ import annotations

import fcntl
import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import manage_guest_cron_launchd as manager


@pytest.fixture
def runtime(tmp_path: Path) -> dict[str, Path]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    executable = tmp_path / "bin" / "hermes"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    profile_home = tmp_path / "profiles" / "guest"
    (profile_home / "cron").mkdir(parents=True)
    (profile_home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (profile_home / ".env").write_text("TEST_KEY=value\n", encoding="utf-8")
    (profile_home / "cron" / "jobs.json").write_text('{"jobs": []}\n', encoding="utf-8")
    return {"checkout": checkout, "executable": executable, "profile_home": profile_home}


def generated(runtime: dict[str, Path]) -> dict[str, Any]:
    return manager.generate_plist(
        hermes_executable=runtime["executable"],
        checkout=runtime["checkout"],
        profile_home=runtime["profile_home"],
        path="/test/bin:/usr/bin",
    )


def test_generate_has_isolated_profile_interval_and_explicit_arguments(runtime: dict[str, Path]) -> None:
    plist = generated(runtime)
    assert plist["Label"] == "ai.hermes.cron-guest"
    assert plist["StartInterval"] == 60
    assert plist["ProcessType"] == "Background"
    arguments = plist["ProgramArguments"]
    assert arguments[2] == "run-once"
    assert arguments[arguments.index("--hermes-executable") + 1] == str(runtime["executable"])
    assert arguments[arguments.index("--checkout") + 1] == str(runtime["checkout"])
    assert arguments[arguments.index("--profile-home") + 1] == str(runtime["profile_home"])
    assert plist["EnvironmentVariables"] == {
        "HERMES_HOME": str(runtime["profile_home"]),
        "HOME": str(runtime["profile_home"]),
        "TMPDIR": str(runtime["profile_home"] / "state" / "tmp"),
        "PYTHONPATH": str(runtime["checkout"]),
        "PATH": "/test/bin:/usr/bin",
    }


@pytest.mark.parametrize("missing", ["config.yaml", ".env", "cron/jobs.json"])
def test_profile_paths_are_required(runtime: dict[str, Path], missing: str) -> None:
    (runtime["profile_home"] / missing).unlink()
    with pytest.raises(ValueError, match="required Guest path"):
        generated(runtime)


def test_rejects_non_guest_profile_and_non_executable(runtime: dict[str, Path]) -> None:
    other = runtime["profile_home"].with_name("poke")
    runtime["profile_home"].rename(other)
    with pytest.raises(ValueError, match="basename"):
        manager.validate_runtime_paths(
            hermes_executable=runtime["executable"], checkout=runtime["checkout"], profile_home=other
        )
    runtime["executable"].chmod(0o600)
    with pytest.raises(ValueError, match="not executable"):
        manager.validate_runtime_paths(
            hermes_executable=runtime["executable"], checkout=runtime["checkout"], profile_home=other.with_name("guest")
        )


def test_run_once_uses_exact_command_environment_cwd_and_timeout(runtime: dict[str, Path]) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 7)

    result = manager.run_once(
        hermes_executable=runtime["executable"],
        checkout=runtime["checkout"],
        profile_home=runtime["profile_home"],
        path="/safe/bin",
        runner=fake_run,
    )
    assert result == 7
    assert calls == [
        (
            [str(runtime["executable"]), "--profile", "guest", "cron", "tick"],
            {
                "cwd": str(runtime["checkout"]),
                "env": {
                    "HERMES_HOME": str(runtime["profile_home"]),
                    "HOME": str(runtime["profile_home"]),
                    "TMPDIR": str(runtime["profile_home"] / "state" / "tmp"),
                    "PYTHONPATH": str(runtime["checkout"]),
                    "PATH": "/safe/bin",
                },
                "check": False,
                "timeout": 50,
            },
        )
    ]


def test_run_once_skips_overlap_without_spawning(runtime: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    state = runtime["profile_home"] / "state"
    state.mkdir()
    lock_path = state / "cron-guest.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def forbidden(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("overlapping tick must not spawn")

        assert manager.run_once(
            hermes_executable=runtime["executable"],
            checkout=runtime["checkout"],
            profile_home=runtime["profile_home"],
            path="/safe/bin",
            runner=forbidden,
        ) == 0
    assert '"status": "overlap-skipped"' in capsys.readouterr().out


def test_install_is_atomic_idempotent_and_calls_expected_launchctl(runtime: dict[str, Path], tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    agents = tmp_path / "LaunchAgents"
    first = manager.install(generated(runtime), launch_agents_dir=agents, runner=fake_run, uid=501)
    second = manager.install(generated(runtime), launch_agents_dir=agents, runner=fake_run, uid=501)
    assert first["changed"] is True
    assert second["changed"] is False
    installed = agents / manager.PLIST_NAME
    manager.validate_plist(plistlib.loads(installed.read_bytes()))
    assert calls[:2] == [
        ["launchctl", "bootstrap", "gui/501", str(installed)],
        ["launchctl", "kickstart", "gui/501/ai.hermes.cron-guest"],
    ]


def test_dry_run_never_writes_or_calls_launchctl(runtime: dict[str, Path], tmp_path: Path) -> None:
    def forbidden(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("dry-run must not call launchctl")

    agents = tmp_path / "LaunchAgents"
    result = manager.install(
        generated(runtime), launch_agents_dir=agents, dry_run=True, runner=forbidden
    )
    assert result["changed"] is True
    assert not agents.exists()


def test_uninstall_is_idempotent(runtime: dict[str, Path], tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    agents = tmp_path / "LaunchAgents"
    manager.install(generated(runtime), launch_agents_dir=agents, load=False)
    assert manager.uninstall(launch_agents_dir=agents, runner=fake_run, uid=501)["removed"] is True
    assert manager.uninstall(launch_agents_dir=agents, runner=fake_run, uid=501)["removed"] is False
    assert calls == [["launchctl", "bootout", "gui/501/ai.hermes.cron-guest"]]


def test_status_is_deterministic_when_absent_malformed_unloaded_and_loaded(
    runtime: dict[str, Path], tmp_path: Path
) -> None:
    agents = tmp_path / "LaunchAgents"
    assert manager.status(launch_agents_dir=agents) == {
        "path": str((agents / manager.PLIST_NAME).resolve()),
        "installed": False,
        "valid": False,
        "loaded": False,
    }
    agents.mkdir()
    installed = agents / manager.PLIST_NAME
    installed.write_bytes(b"not a plist")
    malformed = manager.status(launch_agents_dir=agents)
    assert malformed["installed"] is True
    assert malformed["valid"] is False

    installed.write_bytes(manager.plist_bytes(generated(runtime)))

    def unloaded(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 113, "", "not found")

    assert manager.status(launch_agents_dir=agents, runner=unloaded, uid=501) == {
        "path": str(installed.resolve()), "installed": True, "valid": True, "loaded": False
    }

    def loaded(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, "state = running\npid = 4321\nlast exit code = 0\n", ""
        )

    assert manager.status(launch_agents_dir=agents, runner=loaded, uid=501) == {
        "path": str(installed.resolve()),
        "installed": True,
        "valid": True,
        "loaded": True,
        "state": "running",
        "pid": 4321,
        "last_exit_status": 0,
    }


def test_validate_plist_rejects_cross_profile_environment(runtime: dict[str, Path]) -> None:
    plist = generated(runtime)
    plist["EnvironmentVariables"]["OTHER_PROFILE"] = "/profiles/poke"
    with pytest.raises(ValueError, match="exact Guest runner whitelist"):
        manager.validate_plist(plist)


def test_validate_plist_rejects_home_or_tmpdir_escape(runtime: dict[str, Path]) -> None:
    for key in ("HOME", "TMPDIR"):
        plist = generated(runtime)
        plist["EnvironmentVariables"][key] = "/profiles/poke"
        with pytest.raises(ValueError, match=key):
            manager.validate_plist(plist)


def test_validate_plist_rejects_argument_environment_mismatch(runtime: dict[str, Path]) -> None:
    plist = generated(runtime)
    arguments = plist["ProgramArguments"]
    arguments[arguments.index("--profile-home") + 1] = "/profiles/poke"
    with pytest.raises(ValueError, match="profile-home"):
        manager.validate_plist(plist)
