#!/usr/bin/env python3
"""Dry-run-first atomic installer for proactive profile configuration."""
from __future__ import annotations
import argparse, copy, os, tempfile
from pathlib import Path
import yaml


def _merge(base, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict): _merge(base[key], value)
        else: base[key] = copy.deepcopy(value)


def install(*, profile: str, root: str | None = None, apply: bool = False) -> dict:
    if profile not in {"poke", "guest"}: raise ValueError("profile must be poke or guest")
    home = Path(root).expanduser().resolve() if root else Path.home()/".hermes"/"profiles"/profile
    config_path = home/"config.yaml"
    current = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    template = yaml.safe_load(Path(__file__).with_name("templates").joinpath("proactive-rollout.yaml").read_text())
    merged = copy.deepcopy(current or {}); _merge(merged, template)
    # Guest owns Stephen policy/memory but never a second BlueBubbles adapter.
    if profile == "guest":
        platforms = merged.setdefault("gateway", {}).setdefault("platforms", {})
        platforms.setdefault("bluebubbles", {})["enabled"] = False
    payload = yaml.safe_dump(merged, sort_keys=False)
    result = {"profile": profile, "root": str(home), "apply": apply, "changed": payload != yaml.safe_dump(current or {}, sort_keys=False), "mode": "observe"}
    if not apply: return result
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".config.", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temp, 0o600); os.replace(temp, config_path)
    except BaseException:
        try: os.unlink(temp)
        except OSError: pass
        raise
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--profile",choices=("poke","guest"),required=True); p.add_argument("--root"); p.add_argument("--apply",action="store_true")
    args = p.parse_args(argv)
    print(install(profile=args.profile, root=args.root, apply=args.apply))
    return 0
if __name__ == "__main__": raise SystemExit(main())
