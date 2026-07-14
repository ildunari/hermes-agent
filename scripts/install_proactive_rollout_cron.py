#!/usr/bin/env python3
"""Install proactive maintenance, watchdog, and a proven operator alarm sink."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any

from cron.jobs import create_job, list_jobs, remove_job, update_job, use_cron_store
from scripts.install_contact_memory_maintenance_cron import install as install_maintenance, resolve_profile_home

WATCHDOG_NAME = "Proactive rollout health watchdog"
WATCHDOG_SCRIPT = "proactive_health_watchdog.py"
ALARM_PROBE_NAME = "Proactive alarm sink end-to-end probe"
ALARM_PROBE_SCRIPT = "proactive_alarm_sink_probe.py"
SCHEDULE = "every 30m"
ALARM_PROBE_SCHEDULE = "every 6h"
_SUPPORTED_ALARM_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "email", "bluebubbles", "signal", "matrix",
})


def _runner(root: Path, profile: str) -> str:
    status_script = Path(__file__).with_name("proactive_status.py").resolve()
    return ("#!/usr/bin/env python3\nimport subprocess,sys\n"
            f"p=subprocess.run([sys.executable,{str(status_script)!r},'--profile',{profile!r},'--root',{str(root)!r},'--json','--fail-on-dead'],capture_output=True,text=True)\n"
            "if p.returncode: print(p.stdout.strip() or 'proactive watchdog failed')\n"
            "raise SystemExit(p.returncode)\n")


def _atomic(path: Path, content: str, *, executable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".proactive.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        mode = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if executable else 0)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def validate_alarm_target(target: str) -> dict[str, str]:
    value = str(target or "").strip()
    if ":" not in value or "," in value:
        raise ValueError("alarm target must be one explicit platform:chat target")
    platform, address = value.split(":", 1)
    if platform.lower() not in _SUPPORTED_ALARM_PLATFORMS or not address.strip():
        raise ValueError("alarm target platform is not a supported Hermes delivery sink")
    return {"platform": platform.lower(), "address": address.strip(), "target": value}


def _probe_script(nonce: str, generation: str) -> str:
    return (
        "#!/usr/bin/env python3\n"
        f"print('HERMES_PROACTIVE_ALARM_PROBE_ACK_REQUEST {nonce} {generation}')\n"
    )


def _probe_binding(*, target: str, nonce: str, generation: str, script: str) -> dict[str, str]:
    return {
        "target": target,
        "script": ALARM_PROBE_SCRIPT,
        "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "nonce": nonce,
        "generation": generation,
    }


def install(*, profile: str | None = None, root: str | None = None,
            alarm_target: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    home = resolve_profile_home(profile=profile, root=root)
    profile_name = str(profile or home.name)
    if profile_name not in {"poke", "guest"}:
        raise ValueError("rollout cron is limited to poke and guest")
    target = validate_alarm_target(alarm_target or "")
    plan: dict[str, Any] = {
        "profile_home": str(home), "profile": profile_name, "schedule": SCHEDULE,
        "dry_run": dry_run, "jobs": ["maintenance", "watchdog", "alarm_probe"],
        "alarm_target": target["target"], "task": "proactive_semantic",
    }
    if dry_run:
        return plan

    maintenance = install_maintenance(root=str(home), task="proactive_semantic", dry_run=False)
    _atomic(home / "scripts" / WATCHDOG_SCRIPT, _runner(home, profile_name))
    manifest_path = home / "proactive-alarm-sink.json"
    nonce = secrets.token_hex(32)
    generation = secrets.token_hex(32)
    prior_binding = None
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            prior_nonce = str(previous.get("nonce") or "")
            prior_generation = str(previous.get("generation") or "")
            prior_script = _probe_script(prior_nonce, prior_generation)
            candidate = _probe_binding(
                target=target["target"], nonce=prior_nonce,
                generation=prior_generation, script=prior_script,
            )
            if (
                previous.get("version") == 2
                and previous.get("binding") == candidate
                and prior_nonce and prior_generation
                and (home / "scripts" / ALARM_PROBE_SCRIPT).read_text(encoding="utf-8") == prior_script
            ):
                nonce, generation, prior_binding = prior_nonce, prior_generation, candidate
        except (OSError, ValueError):
            pass
    probe_script = _probe_script(nonce, generation)
    binding = _probe_binding(
        target=target["target"], nonce=nonce, generation=generation, script=probe_script,
    )
    _atomic(home / "scripts" / ALARM_PROBE_SCRIPT, probe_script)
    _atomic(manifest_path, json.dumps({
        "version": 2, "type": "hermes_cron", "target": target["target"],
        "nonce": nonce, "generation": generation, "binding": binding,
        "probe_schedule": ALARM_PROBE_SCHEDULE,
    }, sort_keys=True) + "\n", executable=False)

    desired = {"name": WATCHDOG_NAME, "prompt": "", "schedule": SCHEDULE,
               "script": WATCHDOG_SCRIPT, "no_agent": True,
               "deliver": target["target"], "enabled": True}
    probe_desired = {"name": ALARM_PROBE_NAME, "prompt": "",
                     "schedule": ALARM_PROBE_SCHEDULE, "script": ALARM_PROBE_SCRIPT,
                     "no_agent": True, "deliver": target["target"], "enabled": True,
                     "probe_binding": binding}
    with use_cron_store(home):
        matches = [j for j in list_jobs(include_disabled=True) if j.get("name") == WATCHDOG_NAME]
        if matches:
            job = update_job(matches[0]["id"], desired)
            for duplicate in matches[1:]:
                remove_job(duplicate["id"])
        else:
            job = create_job(prompt=None, name=WATCHDOG_NAME, schedule=SCHEDULE,
                             script=WATCHDOG_SCRIPT, no_agent=True, deliver=target["target"])
        probes = [j for j in list_jobs(include_disabled=True) if j.get("name") == ALARM_PROBE_NAME]
        if probes:
            if probes[0].get("probe_binding") != binding or prior_binding != binding:
                probe_desired["last_probe_delivery_ack"] = None
            probe_job = update_job(probes[0]["id"], probe_desired)
            for duplicate in probes[1:]:
                remove_job(duplicate["id"])
        else:
            probe_job = create_job(prompt=None, name=ALARM_PROBE_NAME,
                                   schedule=ALARM_PROBE_SCHEDULE, script=ALARM_PROBE_SCRIPT,
                                   no_agent=True, deliver=target["target"])
            probe_job = update_job(probe_job["id"], {
                "probe_binding": binding, "last_probe_delivery_ack": None,
            })

    plan.update({
        "dry_run": False, "maintenance_job_id": maintenance["job_id"],
        "watchdog_job_id": job["id"], "alarm_probe_job_id": probe_job["id"],
        "alarm_probe_structurally_verified": bool(
            probe_job.get("deliver") == target["target"]
            and probe_job.get("script") == ALARM_PROBE_SCRIPT
            and job.get("deliver") == target["target"]
        ),
        # This becomes true only after cron transport reports an actual delivery
        # success. Live authorization independently requires this recent ACK.
        "alarm_probe_delivery_ack": bool(
            probe_job.get("last_probe_delivery_ack")
            and probe_job.get("last_probe_delivery_ack", {}).get("target") == target["target"]
            and probe_job.get("last_probe_delivery_ack", {}).get("generation") == generation
        ),
    })
    return plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", choices=("poke", "guest"))
    group.add_argument("--root")
    parser.add_argument("--alarm-target", required=True,
                        help="Explicit supported Hermes target, e.g. telegram:123456")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(install(profile=args.profile, root=args.root, alarm_target=args.alarm_target,
                  dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
