#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


DESCRIPTION = "Run carry gates against the exact staged or pushed Git tree."
ZERO_SHA = "0" * 40


def run(
    root: Path,
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        cwd=root,
        input=input_bytes,
        capture_output=True,
        check=check,
    )


def isolated_tree(
    root: Path,
    commit: str,
) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    holder = tempfile.TemporaryDirectory(prefix="hermes-carry-gate-")
    target = Path(holder.name) / "tree"
    run(root, "git", "clone", "--shared", "--no-checkout", str(root), str(target))
    run(target, "git", "checkout", "--detach", commit)
    return target, holder


def cleanup(holder: tempfile.TemporaryDirectory[str]) -> None:
    holder.cleanup()


def python_for(root: Path) -> str:
    candidate = root / ".venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else shutil.which("python3") or "python3"


def staged(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    target, holder = isolated_tree(root, "HEAD")
    try:
        patch = run(root, "git", "diff", "--cached", "--binary").stdout
        applied = run(
            target,
            "git",
            "apply",
            "--index",
            "--allow-empty",
            "-",
            check=False,
            input_bytes=patch,
        )
        if applied.returncode:
            os.write(2, applied.stderr)
            return applied.returncode
        command = [
            python_for(root),
            str(target / "scripts" / "carry.py"),
            "--root",
            str(target),
            "validate",
        ]
        return subprocess.run(command).returncode
    finally:
        cleanup(holder)


def pushed(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    lines = [line.split() for line in args.stdin.read_text(encoding="utf-8").splitlines()]
    for fields in lines:
        if len(fields) != 4:
            continue
        _, local_sha, _, remote_sha = fields
        if local_sha == ZERO_SHA:
            continue
        target, holder = isolated_tree(root, local_sha)
        try:
            python = python_for(root)
            checks = [
                [
                    python,
                    str(target / "scripts" / "carry.py"),
                    "--root",
                    str(target),
                    "validate",
                ],
                [
                    python,
                    str(target / "scripts" / "thinning.py"),
                    "--root",
                    str(target),
                    "check",
                ],
                [
                    python,
                    str(target / "scripts" / "carry.py"),
                    "--root",
                    str(target),
                    "verify",
                    "--changed-since",
                    remote_sha if remote_sha != ZERO_SHA else "origin/main",
                    "--skip-runtime-probes",
                ],
            ]
            for command in checks:
                result = subprocess.run(command)
                if result.returncode:
                    return result.returncode
        finally:
            cleanup(holder)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=DESCRIPTION)
    result.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = result.add_subparsers(dest="command", required=True)
    staged_gate = sub.add_parser("staged")
    staged_gate.set_defaults(func=staged)
    pushed_gate = sub.add_parser("pushed")
    pushed_gate.add_argument("--stdin", type=Path, default=Path("/dev/stdin"))
    pushed_gate.set_defaults(func=pushed)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
