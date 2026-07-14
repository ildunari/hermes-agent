#!/usr/bin/env python3
"""Idempotently install proactive maintenance and silent health watchdog cron."""
from __future__ import annotations
import argparse, os, stat, tempfile
from pathlib import Path
from typing import Any

from cron.jobs import create_job, list_jobs, remove_job, update_job, use_cron_store
from scripts.install_contact_memory_maintenance_cron import install as install_maintenance, resolve_profile_home

WATCHDOG_NAME = "Proactive rollout health watchdog"
WATCHDOG_SCRIPT = "proactive_health_watchdog.py"
SCHEDULE = "every 30m"


def _runner(root: Path, profile: str) -> str:
    status_script = Path(__file__).with_name("proactive_status.py").resolve()
    return ("#!/usr/bin/env python3\nimport subprocess,sys\n"
            f"p=subprocess.run([sys.executable,{str(status_script)!r},'--profile',{profile!r},'--root',{str(root)!r},'--json','--fail-on-dead'],capture_output=True,text=True)\n"
            "if p.returncode: print(p.stdout.strip() or 'proactive watchdog failed')\n"
            "raise SystemExit(p.returncode)\n")


def _atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".watchdog.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        os.replace(temp, path)
    except BaseException:
        try: os.unlink(temp)
        except OSError: pass
        raise


def install(*, profile: str | None = None, root: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    home = resolve_profile_home(profile=profile, root=root)
    profile_name = str(profile or home.name)
    if profile_name not in {"poke", "guest"}:
        raise ValueError("rollout cron is limited to poke and guest")
    plan: dict[str, Any] = {"profile_home": str(home), "profile": profile_name,
                            "schedule": SCHEDULE, "dry_run": dry_run,
                            "jobs": ["maintenance", "watchdog"], "task": "proactive_semantic"}
    if dry_run: return plan
    maintenance = install_maintenance(root=str(home), task="proactive_semantic", dry_run=False)
    _atomic(home/"scripts"/WATCHDOG_SCRIPT, _runner(home, profile_name))
    desired = {"name": WATCHDOG_NAME, "prompt": "", "schedule": SCHEDULE,
               "script": WATCHDOG_SCRIPT, "no_agent": True, "deliver": "local", "enabled": True}
    with use_cron_store(home):
        matches = [j for j in list_jobs(include_disabled=True) if j.get("name") == WATCHDOG_NAME]
        if matches:
            job = update_job(matches[0]["id"], desired)
            for duplicate in matches[1:]: remove_job(duplicate["id"])
        else:
            job = create_job(prompt=None, name=WATCHDOG_NAME, schedule=SCHEDULE,
                             script=WATCHDOG_SCRIPT, no_agent=True, deliver="local")
    plan.update({"dry_run": False, "maintenance_job_id": maintenance["job_id"],
                 "watchdog_job_id": job["id"]})
    return plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", choices=("poke", "guest")); group.add_argument("--root")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(install(profile=args.profile, root=args.root, dry_run=args.dry_run))
    return 0

if __name__ == "__main__": raise SystemExit(main())
