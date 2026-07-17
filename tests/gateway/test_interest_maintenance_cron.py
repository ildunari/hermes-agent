from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cron.jobs import list_jobs, use_cron_store
from cron import scheduler
from gateway.contact_memory.store import ContactMemoryStore
from scripts import contact_memory_interest_maintenance as runner
from scripts import install_contact_memory_maintenance_cron as installer


def _jobs(home: Path) -> list[dict]:
    with use_cron_store(home):
        return list_jobs(include_disabled=True)


def test_install_is_idempotent_profile_isolated_and_exact(tmp_path, monkeypatch):
    live_decoy = tmp_path / "must-not-be-touched"
    profile = tmp_path / "profiles" / "poke"
    other = tmp_path / "profiles" / "other"
    monkeypatch.setenv("HERMES_HOME", str(live_decoy))

    first = installer.install(root=str(profile))
    second = installer.install(root=str(profile))

    assert first["job_id"] == second["job_id"]
    jobs = _jobs(profile)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["name"] == installer.JOB_NAME
    assert job["no_agent"] is True
    assert job["script"] == installer.JOB_SCRIPT
    assert job["schedule"]["kind"] == "interval"
    assert job["schedule"]["minutes"] == 30
    assert job["schedule_display"] == installer.JOB_SCHEDULE
    assert job["deliver"] == "local"
    assert not _jobs(other)
    assert not live_decoy.exists()

    installed = profile / "scripts" / installer.JOB_SCRIPT
    assert installed.is_file()
    installed_source = installed.read_text(encoding="utf-8")
    assert str((profile / "contact-memory").resolve()) in installed_source
    assert "INSTALLED_TASK: str | None = 'monitor'" in installed_source
    assert f"INSTALLED_SOURCE_ROOT: str | None = {str(Path(installer.__file__).resolve().parents[1])!r}" in installed_source


def test_dry_run_does_not_write(tmp_path):
    profile = tmp_path / "dry-profile"
    result = installer.install(root=str(profile), dry_run=True)
    assert result["dry_run"] is True
    assert result["no_agent"] is True
    assert result["schedule"] == "every 30m"
    assert result["task"] == "monitor"
    assert not profile.exists()


def test_installed_no_agent_script_executes_silently_through_scheduler(
    tmp_path, monkeypatch,
):
    profile = tmp_path / "scheduled-profile"
    ambient_decoy = tmp_path / "ambient-profile"
    import_decoy = tmp_path / "import-decoy"
    (import_decoy / "gateway").mkdir(parents=True)
    (import_decoy / "gateway" / "__init__.py").write_text(
        "raise RuntimeError('cross-checkout gateway import')\n", encoding="utf-8"
    )
    installer.install(root=str(profile))
    # A profile-scoped scheduler may use a context/module override while the
    # process-global environment still points at another live profile.  The
    # child must inherit the same profile used to resolve its script.
    monkeypatch.setenv("HERMES_HOME", str(ambient_decoy))
    monkeypatch.setenv("PYTHONPATH", str(import_decoy))
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: profile)

    success, output = scheduler._run_job_script(installer.JOB_SCRIPT)
    assert (success, output) == (True, "")
    assert not ambient_decoy.exists()


def test_installed_runner_rejects_an_invalid_pinned_checkout(tmp_path):
    profile = tmp_path / "scheduled-profile"
    installer.install(root=str(profile))
    installed = profile / "scripts" / installer.JOB_SCRIPT
    source = installed.read_text(encoding="utf-8")
    pinned = str(Path(installer.__file__).resolve().parents[1])
    installed.write_text(source.replace(repr(pinned), repr(str(tmp_path / "missing"))), encoding="utf-8")

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(installed)], capture_output=True, text=True,
        cwd=tmp_path, env=env,
    )
    assert result.returncode != 0
    assert "installed Hermes source root is invalid" in result.stderr
    assert "No module named 'gateway'" not in result.stderr


def test_runner_enumerates_profile_and_stays_silent_on_success(tmp_path, monkeypatch, capsys):
    root = tmp_path / "contact-memory"
    ContactMemoryStore(root, "telegram:111")
    ContactMemoryStore(root, "discord:222")
    seen: list[str] = []

    async def fake_maintenance(store, *, root, model=None, force=False):
        seen.append(store.contact_namespace)
        return type(
            "Result",
            (),
            {
                "__dict__": {
                    "ran": True,
                    "reason": "test",
                    "compacted": 0,
                    "decayed": 0,
                    "digested": 0,
                    "pruned": 0,
                    "digest_path": None,
                }
            },
        )()

    monkeypatch.setattr(
        "gateway.contact_memory.interest_maintenance.run_maintenance", fake_maintenance
    )
    assert runner.main(["--root", str(root), "--task", "monitor"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert len(seen) == 2


def test_runner_reports_errors_only_on_stderr(tmp_path, monkeypatch, capsys):
    async def failed(_root, *, model=None):
        return [{"contact_namespace": "opaque", "error": "boom"}]

    monkeypatch.setattr(runner, "run_profile_maintenance", failed)
    assert runner.main(["--root", str(tmp_path), "--task", "monitor"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "opaque" in captured.err
    assert "boom" in captured.err


def test_runner_uses_configured_auxiliary_monitor_route_and_allows_deterministic_mode(
    tmp_path, monkeypatch, capsys,
):
    seen: dict[str, object] = {}
    sentinel = object()

    def auxiliary(task):
        seen["task"] = task
        return sentinel

    async def profile(_root, *, model=None):
        seen.setdefault("models", []).append(model)
        return []

    monkeypatch.setattr(runner, "_auxiliary_maintenance_model", auxiliary)
    monkeypatch.setattr(runner, "run_profile_maintenance", profile)
    assert runner.main(["--root", str(tmp_path), "--task", "taxonomy-maintenance"]) == 0
    assert runner.main(["--root", str(tmp_path), "--deterministic-only"]) == 0
    assert seen == {"task": "taxonomy-maintenance", "models": [sentinel, None]}
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_runner_requires_explicit_task_outside_installed_or_deterministic_mode(
    tmp_path, capsys,
):
    with pytest.raises(SystemExit) as exc:
        runner.main(["--root", str(tmp_path)])
    assert exc.value.code == 2
    assert "--task is required" in capsys.readouterr().err
