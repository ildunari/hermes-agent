#!/usr/bin/env python3
"""Print text-free proactive status for one rollout profile."""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from gateway.proactive_status import health_snapshot  # noqa: E402
from gateway.proactive_scheduler import ProactiveOwnershipRegistry  # noqa: E402


def _cron_fresh(root: Path) -> bool:
    from cron.jobs import list_jobs, use_cron_store
    with use_cron_store(root):
        jobs = list_jobs(include_disabled=True)
    required = [job for job in jobs if job.get("name") in {
        "Proactive rollout health watchdog", "Contact memory interest maintenance"
    }]
    if len(required) != 2 or any(not job.get("enabled", True) for job in required):
        return False
    now = datetime.now(timezone.utc)
    for job in required:
        value = job.get("last_run_at")
        if not value:
            return False
        try:
            if (now - datetime.fromisoformat(str(value))).total_seconds() > 3900:
                return False
        except (TypeError, ValueError):
            return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("poke", "guest"), required=True)
    parser.add_argument("--root")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-dead", action="store_true")
    parser.add_argument("--reset-circuit", action="store_true")
    parser.add_argument("--confirm-reset", action="store_true")
    args = parser.parse_args(argv)
    if args.confirm_reset and not args.reset_circuit:
        parser.error("--confirm-reset requires --reset-circuit")
    if args.reset_circuit:
        if args.root:
            parser.error("circuit reset requires the canonical profile root")
        if not args.confirm_reset:
            parser.error("circuit reset requires --confirm-reset")
        from hermes_cli.profiles import get_profile_dir
        canonical = Path(get_profile_dir(args.profile)).expanduser().resolve()
        ownership = canonical.parent.parent / "proactive-contact-ownership.db"
        ProactiveOwnershipRegistry(ownership).operator_reset_circuit(
            confirmed=True, now=datetime.now(timezone.utc).timestamp()
        )
        print(f"{args.profile}: global proactive circuit reset by operator")
        return 0
    root = Path(args.root).expanduser().resolve() if args.root else Path.home()/".hermes"/"profiles"/args.profile
    config_path = root/"config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    status = health_snapshot(profile_home=root, profile=args.profile, config=config or {},
                             cron_fresh=_cron_fresh(root))
    if args.json:
        print(json.dumps(status, sort_keys=True, indent=2))
    else:
        print(f"{args.profile}: mode={status['mode']} dead={status['dead']} reasons={','.join(status['reasons']) or 'none'}")
    return 2 if args.fail_on_dead and status["dead"] else 0

if __name__ == "__main__": raise SystemExit(main())
