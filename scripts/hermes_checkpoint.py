#!/usr/bin/env python3
"""Create recoverable Hermes worktree snapshots without moving the live branch."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REF_PREFIX = "refs/hermes/checkpoints"
DEFAULT_MAX_FILE = 5 * 1024 * 1024
DEFAULT_MAX_TOTAL = 25 * 1024 * 1024
SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "secrets.json",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
CONFIG_SUFFIXES = {".conf", ".ini", ".json", ".toml", ".txt", ".yaml", ".yml"}
SENSITIVE_PATH_WORDS = {"auth", "credential", "oauth", "secret", "token"}
SECRET_RE = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{24,}|"
    rb"xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[A-Z0-9]{16}|"
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)
STRUCTURED_SECRET_RE = re.compile(
    rb"(?i)[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    rb"client[_-]?secret|private[_-]?key|password)[\"']?\s*[:=]\s*"
    rb"[\"']?[A-Za-z0-9_./+=:-]{12,}"
)


@dataclass(frozen=True)
class Omission:
    path: str
    reason: str
    content_class: str


def run(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return run(repo, "git", *args, env=env).stdout.strip()


def git_dir(repo: Path) -> Path:
    raw = git(repo, "rev-parse", "--git-dir")
    path = Path(raw)
    return path if path.is_absolute() else (repo / path).resolve()


def changed_paths(repo: Path) -> list[str]:
    tracked = git(repo, "diff", "--name-only", "-z", "HEAD").split("\0")
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted({path for path in tracked + untracked if path})


def classify_path(repo: Path, rel: str, max_file: int) -> Omission | None:
    path = repo / rel
    name = path.name.lower()
    if name in SENSITIVE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
        return Omission(rel, "sensitive_path", "credential-like")
    if path.suffix.lower() in CONFIG_SUFFIXES and any(
        word in path.stem.lower() for word in SENSITIVE_PATH_WORDS
    ):
        return Omission(rel, "sensitive_path", "credential-like")
    if not path.exists() or not path.is_file():
        return None
    size = path.stat().st_size
    if size > max_file:
        return Omission(rel, "file_too_large", "oversized")
    try:
        data = path.read_bytes()
    except OSError:
        return Omission(rel, "unreadable", "unknown")
    if SECRET_RE.search(data) or (
        path.suffix.lower() in CONFIG_SUFFIXES and STRUCTURED_SECRET_RE.search(data)
    ):
        return Omission(rel, "secret_pattern", "credential-like")
    return None


def metadata_dir(repo: Path) -> Path:
    path = git_dir(repo) / "hermes-checkpoints"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_metadata(repo: Path, run_id: str, payload: dict[str, object]) -> Path:
    target = metadata_dir(repo) / f"{run_id}.json"
    temp = target.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, target)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return target


def snapshot(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    head = git(repo, "rev-parse", "HEAD")
    branch = git(repo, "branch", "--show-current")
    paths = changed_paths(repo)
    if not paths:
        print(json.dumps({"status": "COMPLETE", "reason": "clean", "head": head}))
        return 0

    omissions: list[Omission] = []
    allowed: list[str] = []
    total = 0
    for rel in paths:
        omission = classify_path(repo, rel, args.max_file_bytes)
        path = repo / rel
        if omission is None and path.is_file():
            total += path.stat().st_size
            if total > args.max_total_bytes:
                omission = Omission(rel, "snapshot_too_large", "oversized")
        if omission:
            omissions.append(omission)
        else:
            allowed.append(rel)

    now = dt.datetime.now(dt.UTC)
    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    ref = f"{REF_PREFIX}/{run_id}"
    if not allowed:
        payload = {
            "status": "FAILED",
            "run_id": run_id,
            "head": head,
            "branch": branch,
            "ref": None,
            "omissions": [item.__dict__ for item in omissions],
        }
        write_metadata(repo, run_id, payload)
        print(json.dumps(payload, sort_keys=True))
        return 2

    with tempfile.TemporaryDirectory(prefix="hermes-checkpoint-") as tmp:
        index = Path(tmp) / "index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index)
        git(repo, "read-tree", "HEAD", env=env)
        for rel in allowed:
            git(repo, "add", "--all", "--", rel, env=env)
        tree = git(repo, "write-tree", env=env)
        message = f"checkpoint: {branch or 'detached'} {run_id}"
        commit = git(repo, "commit-tree", tree, "-p", head, "-m", message)
        git(repo, "update-ref", "-m", message, ref, commit)

    status = "PARTIAL" if omissions else "COMPLETE"
    payload = {
        "status": status,
        "run_id": run_id,
        "head": head,
        "branch": branch,
        "commit": commit,
        "ref": ref,
        "included_count": len(allowed),
        "omissions": [item.__dict__ for item in omissions],
    }
    path = write_metadata(repo, run_id, payload)
    payload["metadata"] = str(path)
    print(json.dumps(payload, sort_keys=True))
    return 1 if omissions else 0


def list_snapshots(args: argparse.Namespace) -> int:
    output = git(args.repo, "for-each-ref", "--format=%(refname) %(objectname)", REF_PREFIX)
    print(output)
    return 0


def show_snapshot(args: argparse.Namespace) -> int:
    ref = resolve_ref(args.snapshot)
    print(git(args.repo, "show", "--stat", "--oneline", "--decorate", ref))
    return 0


def resolve_ref(value: str) -> str:
    return value if value.startswith("refs/") else f"{REF_PREFIX}/{value}"


def restore(args: argparse.Namespace) -> int:
    target = args.to_worktree.expanduser().resolve()
    if target.exists():
        print(f"restore target already exists: {target}", file=sys.stderr)
        return 2
    ref = resolve_ref(args.snapshot)
    git(args.repo, "rev-parse", "--verify", ref)
    git(args.repo, "worktree", "add", "--detach", str(target), ref)
    print(json.dumps({"status": "COMPLETE", "ref": ref, "worktree": str(target)}))
    return 0


def protected_refs(repo: Path) -> set[str]:
    protected: set[str] = set()
    ledger = git_dir(repo) / "hermes-update" / "run.json"
    if ledger.is_file():
        try:
            data = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return protected
        if str(data.get("status", "")).lower() not in {"completed", "failed", "aborted"}:
            for key in ("checkpoint_ref", "rollback_ref"):
                value = data.get(key)
                if isinstance(value, str):
                    protected.add(value)
            for value in data.get("protected_refs", []):
                if isinstance(value, str):
                    protected.add(value)
    return protected


def prune(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    now = dt.datetime.now(dt.UTC)
    rows = git(
        repo,
        "for-each-ref",
        "--sort=-creatordate",
        "--format=%(refname)|%(creatordate:iso-strict)",
        REF_PREFIX,
    ).splitlines()
    protected = protected_refs(repo)
    removed: list[str] = []
    for index, row in enumerate(rows):
        ref, _, raw_date = row.partition("|")
        if index < args.keep_min or ref in protected:
            continue
        try:
            created = dt.datetime.fromisoformat(raw_date)
        except ValueError:
            continue
        if (now - created).days < args.keep_days:
            continue
        git(repo, "update-ref", "-d", ref)
        removed.append(ref)
    print(json.dumps({"status": "COMPLETE", "removed": removed, "protected": sorted(protected)}))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    sub = result.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE)
    snap.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL)
    snap.set_defaults(func=snapshot)
    listing = sub.add_parser("list")
    listing.set_defaults(func=list_snapshots)
    showing = sub.add_parser("show")
    showing.add_argument("snapshot")
    showing.set_defaults(func=show_snapshot)
    restoring = sub.add_parser("restore")
    restoring.add_argument("snapshot")
    restoring.add_argument("--to-worktree", type=Path, required=True)
    restoring.set_defaults(func=restore)
    pruning = sub.add_parser("prune")
    pruning.add_argument("--keep-days", type=int, default=14)
    pruning.add_argument("--keep-min", type=int, default=20)
    pruning.set_defaults(func=prune)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
