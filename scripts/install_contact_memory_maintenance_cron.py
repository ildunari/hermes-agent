#!/usr/bin/env python3
"""Install the profile-owned contact-memory maintenance cron job."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Sequence

from cron.jobs import create_job, list_jobs, remove_job, update_job, use_cron_store

JOB_NAME = "Contact memory interest maintenance"
JOB_SCHEDULE = "every 30m"
JOB_SCRIPT = "contact_memory_interest_maintenance.py"
JOB_DELIVERY = "local"
JOB_TASK = "monitor"
_ROOT_MARKER = "INSTALLED_ROOT: str | None = None"
_TASK_MARKER = "INSTALLED_TASK: str | None = None"
_SOURCE_ROOT_MARKER = "INSTALLED_SOURCE_ROOT: str | None = None"


def resolve_profile_home(*, root: str | None = None, profile: str | None = None) -> Path:
    """Resolve one explicit profile home without consulting the active profile."""
    if bool(root) == bool(profile):
        raise ValueError("exactly one of --root or --profile is required")
    if root:
        return Path(root).expanduser().resolve()
    from hermes_cli.profiles import get_profile_dir

    return get_profile_dir(str(profile)).expanduser().resolve()


def _installed_runner_source(contact_root: Path, *, task: str) -> str:
    source_path = Path(__file__).with_name(JOB_SCRIPT)
    source_root = Path(__file__).resolve().parents[1]
    source = source_path.read_text(encoding="utf-8")
    markers = (_ROOT_MARKER, _TASK_MARKER, _SOURCE_ROOT_MARKER)
    if any(source.count(marker) != 1 for marker in markers):
        raise RuntimeError(f"runner installation markers are missing or ambiguous in {source_path}")
    source = source.replace(
        _ROOT_MARKER, f"INSTALLED_ROOT: str | None = {str(contact_root)!r}"
    )
    source = source.replace(_TASK_MARKER, f"INSTALLED_TASK: str | None = {task!r}")
    return source.replace(
        _SOURCE_ROOT_MARKER,
        f"INSTALLED_SOURCE_ROOT: str | None = {str(source_root)!r}",
    )


def _write_runner(profile_home: Path, content: str) -> Path:
    scripts_dir = profile_home / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    destination = scripts_dir / JOB_SCRIPT
    if destination.is_file() and destination.read_text(encoding="utf-8") == content:
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        return destination

    fd, temporary = tempfile.mkstemp(prefix=f".{JOB_SCRIPT}.", dir=scripts_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o700)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def install(
    *, root: str | None = None, profile: str | None = None,
    task: str = JOB_TASK, dry_run: bool = False,
) -> dict[str, Any]:
    """Install or reconcile exactly one maintenance job in one cron store."""
    profile_home = resolve_profile_home(root=root, profile=profile)
    contact_root = (profile_home / "contact-memory").resolve()
    selected_task = str(task or "").strip()
    if not selected_task:
        raise ValueError("maintenance auxiliary task is required")
    runner_content = _installed_runner_source(contact_root, task=selected_task)

    plan: dict[str, Any] = {
        "profile_home": str(profile_home),
        "contact_root": str(contact_root),
        "script": JOB_SCRIPT,
        "schedule": JOB_SCHEDULE,
        "task": selected_task,
        "no_agent": True,
        "deliver": JOB_DELIVERY,
        "dry_run": dry_run,
    }
    if dry_run:
        return plan

    _write_runner(profile_home, runner_content)
    desired = {
        "name": JOB_NAME,
        "prompt": "",
        "schedule": JOB_SCHEDULE,
        "script": JOB_SCRIPT,
        "no_agent": True,
        "deliver": JOB_DELIVERY,
        "enabled": True,
    }
    with use_cron_store(profile_home):
        matches = [job for job in list_jobs(include_disabled=True) if job.get("name") == JOB_NAME]
        if matches:
            job = update_job(matches[0]["id"], desired)
            if job is None:  # defensive: the store changed unexpectedly
                raise RuntimeError("maintenance cron job disappeared during reconciliation")
            for duplicate in matches[1:]:
                remove_job(duplicate["id"])
        else:
            job = create_job(
                prompt=None,
                name=JOB_NAME,
                schedule=JOB_SCHEDULE,
                script=JOB_SCRIPT,
                no_agent=True,
                deliver=JOB_DELIVERY,
            )
    plan.update({"job_id": job["id"], "dry_run": False})
    return plan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install profile-owned contact-memory maintenance cron")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--root", help="explicit Hermes profile home")
    selection.add_argument("--profile", help="explicit named Hermes profile")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--task", default=JOB_TASK, help="configured auxiliary-model task")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = install(
        root=args.root, profile=args.profile, task=args.task, dry_run=args.dry_run
    )
    action = "Would install" if args.dry_run else "Installed"
    print(f"{action} {JOB_NAME!r} in {result['profile_home']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
