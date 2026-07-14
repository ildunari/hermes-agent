#!/usr/bin/env python3
"""Print text-free proactive status for one rollout profile."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from gateway.proactive_status import health_snapshot  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("poke", "guest"), required=True)
    parser.add_argument("--root")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-dead", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve() if args.root else Path.home()/".hermes"/"profiles"/args.profile
    config_path = root/"config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    status = health_snapshot(profile_home=root, profile=args.profile, config=config or {})
    if args.json:
        print(json.dumps(status, sort_keys=True, indent=2))
    else:
        print(f"{args.profile}: mode={status['mode']} dead={status['dead']} reasons={','.join(status['reasons']) or 'none'}")
    return 2 if args.fail_on_dead and status["dead"] else 0

if __name__ == "__main__": raise SystemExit(main())
