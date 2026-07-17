#!/usr/bin/env python3
"""Silent no-agent runner for profile-scoped contact-memory maintenance.

The source runner requires an explicit ``--root`` or ``--profile``.  The cron
installer writes a profile-owned copy with ``INSTALLED_ROOT`` pinned to that
profile's contact-memory directory, avoiding dependence on the scheduler's
ambient profile or working directory.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Sequence

# Replaced in the profile-owned copy by install_contact_memory_maintenance_cron.py.
INSTALLED_ROOT: str | None = None
INSTALLED_TASK: str | None = None
INSTALLED_SOURCE_ROOT: str | None = None


def _bootstrap_installed_source_root() -> None:
    """Import an installed runner from the checkout which installed it.

    Profile scripts live outside the Python source tree, and cron intentionally
    does not rely on an ambient ``PYTHONPATH``.  The installer pins its own
    resolved checkout root here.  Validate the pin before putting it first on
    ``sys.path`` so a stale/tampered runner fails closed instead of importing a
    same-named package from another Hermes checkout.
    """
    if INSTALLED_SOURCE_ROOT is None:
        return
    source_root = Path(INSTALLED_SOURCE_ROOT).expanduser().resolve()
    required = (
        source_root / "gateway" / "__init__.py",
        source_root / "cron" / "scheduler.py",
        source_root / "scripts" / "contact_memory_interest_maintenance.py",
    )
    if not all(path.is_file() for path in required):
        raise RuntimeError(f"installed Hermes source root is invalid: {source_root}")
    source = str(source_root)
    if not sys.path or sys.path[0] != source:
        try:
            sys.path.remove(source)
        except ValueError:
            pass
        sys.path.insert(0, source)


_bootstrap_installed_source_root()

from gateway.contact_memory.admin import resolve_contact_memory_root  # noqa: E402
from gateway.contact_memory.interest_maintenance import (  # noqa: E402
    _auxiliary_maintenance_model,
    run_profile_maintenance,
)


def _resolve_root(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Path:
    if args.root or args.profile:
        try:
            return resolve_contact_memory_root(root=args.root, profile=args.profile).expanduser().resolve()
        except ValueError as exc:
            parser.error(str(exc))
    if INSTALLED_ROOT:
        return Path(INSTALLED_ROOT).expanduser().resolve()
    parser.error("one of --root or --profile is required")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Silently maintain all contact-memory stores in one explicit profile"
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--root", help="explicit contact-memory root")
    selection.add_argument("--profile", help="explicit named Hermes profile")
    parser.add_argument(
        "--task",
        help="explicit configured auxiliary-model task (for example: monitor)",
    )
    parser.add_argument(
        "--deterministic-only",
        action="store_true",
        help="fold/lifecycle/digest only; defer auxiliary taxonomy proposals",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = _resolve_root(args, parser)
    task = str(args.task or INSTALLED_TASK or "").strip()
    if not args.deterministic_only and not task:
        parser.error("--task is required unless --deterministic-only is set")

    try:
        # The scheduled path uses the explicitly selected Hermes auxiliary task,
        # not an ad-hoc SDK/provider. It is noninteractive and follows the same
        # provider/auth/fallback architecture as other background classifiers.
        model = None if args.deterministic_only else _auxiliary_maintenance_model(task)
        results = asyncio.run(run_profile_maintenance(root, model=model))
    except Exception as exc:
        print(f"contact-memory maintenance failed: {exc}", file=sys.stderr)
        return 1

    errors = [result for result in results if result.get("error")]
    if errors:
        for result in errors:
            namespace = result.get("contact_namespace", "unknown")
            print(f"contact-memory maintenance failed for {namespace}: {result['error']}", file=sys.stderr)
        return 1

    # no work and successful work are deliberately silent: empty stdout means
    # the no-agent cron scheduler has nothing to deliver.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
