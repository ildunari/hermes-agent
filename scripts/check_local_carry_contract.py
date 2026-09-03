#!/usr/bin/env python3
"""Validate the thin local carry against its ownership manifest and limits."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


VALID_DISPOSITIONS = {
    "delete-stale",
    "plugin",
    "ops-support",
    "upstream",
    "must-carry",
}
EVIDENCE_FIELDS = (
    "registration",
    "runtime_caller",
    "interface_test",
    "artifact",
)


class CarryContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class CarryMetrics:
    paths: int
    insertions: int
    deletions: int
    total_changed_lines: int
    modified_upstream_lines: int


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        raise CarryContractError(result.stderr.strip() or "git command failed")
    return result.stdout


def _changed_rows(repo: Path, base_ref: str) -> dict[str, tuple[int, int]]:
    rows: dict[str, tuple[int, int]] = {}
    output = _git(repo, "diff", "--no-renames", "--numstat", f"{base_ref}...HEAD")
    for raw in output.splitlines():
        added, deleted, path = raw.split("\t", 2)
        # Binary paths use '-'; they still count as paths but not text lines.
        rows[path] = (
            0 if added == "-" else int(added),
            0 if deleted == "-" else int(deleted),
        )
    return rows


def _exists_at(repo: Path, base_ref: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{base_ref}:{path}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def measure(repo: Path, base_ref: str) -> tuple[CarryMetrics, set[str]]:
    rows = _changed_rows(repo, base_ref)
    insertions = sum(value[0] for value in rows.values())
    deletions = sum(value[1] for value in rows.values())
    modified_upstream_lines = sum(
        added + deleted
        for path, (added, deleted) in rows.items()
        if _exists_at(repo, base_ref, path)
    )
    return (
        CarryMetrics(
            paths=len(rows),
            insertions=insertions,
            deletions=deletions,
            total_changed_lines=insertions + deletions,
            modified_upstream_lines=modified_upstream_lines,
        ),
        set(rows),
    )


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CarryContractError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise CarryContractError("manifest version must be 1")
    return data


def validate(
    repo: Path,
    manifest_path: Path,
    base_ref: str | None = None,
    support_root: Path | None = None,
) -> CarryMetrics:
    manifest = load_manifest(manifest_path)
    resolved_base = str(base_ref or manifest.get("base_ref") or "origin/main")
    metrics, changed_paths = measure(repo, resolved_base)

    raw_entries = manifest.get("paths")
    if not isinstance(raw_entries, list):
        raise CarryContractError("manifest paths must be a list")

    entries: dict[str, dict[str, Any]] = {}
    for row in raw_entries:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise CarryContractError("every path entry must be a mapping with path")
        path = row["path"]
        if path in entries:
            raise CarryContractError(f"duplicate manifest path: {path}")
        disposition = row.get("disposition")
        if disposition not in VALID_DISPOSITIONS:
            raise CarryContractError(f"invalid disposition for {path}: {disposition}")
        if not isinstance(row.get("owner"), str) or not row["owner"].strip():
            raise CarryContractError(f"missing owner for {path}")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            raise CarryContractError(f"missing evidence for {path}")
        missing = [field for field in EVIDENCE_FIELDS if not str(evidence.get(field) or "").strip()]
        if missing:
            raise CarryContractError(f"missing evidence for {path}: {', '.join(missing)}")
        entries[path] = row

    manifest_paths = set(entries)
    unknown = sorted(changed_paths - manifest_paths)
    stale = sorted(manifest_paths - changed_paths)
    if unknown:
        raise CarryContractError(f"unclassified carry paths: {', '.join(unknown)}")
    if stale:
        raise CarryContractError(f"stale manifest paths: {', '.join(stale)}")

    limits = manifest.get("limits")
    if not isinstance(limits, dict):
        raise CarryContractError("manifest limits must be a mapping")
    for field in ("paths", "total_changed_lines", "modified_upstream_lines"):
        limit = limits.get(field)
        if not isinstance(limit, int) or limit < 0:
            raise CarryContractError(f"limit {field} must be a non-negative integer")
        actual = getattr(metrics, field)
        if actual > limit:
            raise CarryContractError(f"{field} regressed: {actual} > {limit}")

    if support_root is not None:
        required_support = manifest.get("required_support")
        if not isinstance(required_support, list) or not required_support:
            raise CarryContractError("required_support must be a non-empty list")
        missing_support = []
        for item in required_support:
            if not isinstance(item, str) or not item.strip():
                missing_support.append(str(item))
                continue
            if not (support_root / item).is_file():
                missing_support.append(item)
        missing_support.sort()
        if missing_support:
            raise CarryContractError(
                f"missing required support: {', '.join(missing_support)}"
            )
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--manifest", type=Path, default=Path("scripts/local_carry_manifest.yaml")
    )
    parser.add_argument("--base-ref")
    parser.add_argument(
        "--support-root",
        type=Path,
        help="verify external support files under this plugin repository root",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    manifest = args.manifest
    if not manifest.is_absolute():
        manifest = args.repo / manifest
    try:
        metrics = validate(
            args.repo.resolve(), manifest, args.base_ref, args.support_root
        )
    except CarryContractError as exc:
        print(f"carry contract: FAIL: {exc}", file=sys.stderr)
        return 1
    payload = {"status": "PASS", **metrics.__dict__}
    print(json.dumps(payload, sort_keys=True) if args.json else f"carry contract: PASS {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
