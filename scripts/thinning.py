#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


DESCRIPTION = "Measure and ratchet upstream collision cost for local/studio-slim."
GENERATED_STATE_PATHS = {"scripts/thinning_baseline.json"}


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )
    return result.stdout.strip()


def changed_lines(root: Path, merge_base: str, path: str) -> int:
    output = git(root, "diff", "--numstat", f"{merge_base}...HEAD", "--", path)
    total = 0
    for line in output.splitlines():
        added, deleted, *_ = line.split("\t")
        if added.isdigit():
            total += int(added)
        if deleted.isdigit():
            total += int(deleted)
    return total


def upstream_churn(root: Path, merge_base: str, upstream: str, path: str) -> int:
    output = git(root, "rev-list", f"{merge_base}..{upstream}", "--", path)
    return len(output.splitlines()) if output else 0


def conflict_counts(root: Path) -> dict[str, int]:
    raw_git_dir = Path(git(root, "rev-parse", "--git-dir"))
    git_dir = raw_git_dir if raw_git_dir.is_absolute() else root / raw_git_dir
    counts: dict[str, int] = {}
    update_dir = git_dir / "hermes-update"
    if not update_dir.is_dir():
        return counts
    for ledger in update_dir.glob("**/*.json"):
        try:
            data = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        paths = data.get("conflict_files") or data.get("artifacts", {}).get("conflict_files", [])
        for path in paths:
            if isinstance(path, str):
                counts[path] = counts.get(path, 0) + 1
    return counts


def compute(root: Path, upstream: str) -> dict[str, object]:
    merge_base = git(root, "merge-base", "HEAD", upstream)
    changed = git(root, "diff", "--name-only", f"{merge_base}...HEAD").splitlines()
    upstream_files = set(git(root, "ls-tree", "-r", "--name-only", upstream).splitlines())
    conflicts = conflict_counts(root)
    hotspots: list[dict[str, object]] = []
    measured_paths = set(changed).intersection(upstream_files) - GENERATED_STATE_PATHS
    for path in sorted(measured_paths):
        local_lines = changed_lines(root, merge_base, path)
        churn = upstream_churn(root, merge_base, upstream, path)
        prior_conflicts = conflicts.get(path, 0)
        score = local_lines * (1 + churn) * (1 + prior_conflicts)
        hotspots.append(
            {
                "path": path,
                "local_changed_lines": local_lines,
                "upstream_commits": churn,
                "prior_conflicts": prior_conflicts,
                "score": score,
            }
        )
    hotspots.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return {
        "version": 1,
        "branch": git(root, "branch", "--show-current"),
        "head": git(root, "rev-parse", "HEAD"),
        "upstream": upstream,
        "upstream_sha": git(root, "rev-parse", upstream),
        "merge_base": merge_base,
        "hotspot_count": len(hotspots),
        "weighted_score": sum(int(item["score"]) for item in hotspots),
        "hotspots": hotspots,
    }


def baseline(args: argparse.Namespace) -> int:
    payload = compute(args.root.resolve(), args.upstream)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"Thinning baseline: {payload['hotspot_count']} hotspots, "
        f"weighted_score={payload['weighted_score']}"
    )
    return 0


def check(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    prior = json.loads(args.baseline.read_text(encoding="utf-8"))
    # Score against the upstream commit the baseline was computed from, not
    # the live ref: otherwise every upstream fetch inflates the churn
    # multiplier on all hotspots and the ratchet fails pushes whose local
    # carry did not grow at all.
    upstream = str(prior.get("upstream_sha") or prior["upstream"])
    current = compute(root, upstream)
    prior_paths = {item["path"] for item in prior["hotspots"]}
    current_paths = {item["path"] for item in current["hotspots"]}
    new_paths = sorted(current_paths - prior_paths)
    score_growth = int(current["weighted_score"]) - int(prior["weighted_score"])
    allowed_growth = int(int(prior["weighted_score"]) * args.tolerance_pct / 100.0)
    if new_paths or score_growth > allowed_growth:
        print("Thinning ratchet: FAIL", file=sys.stderr)
        if new_paths:
            print(f"- new hotspots: {', '.join(new_paths)}", file=sys.stderr)
        if score_growth > allowed_growth:
            print(
                f"- weighted score grew by {score_growth} "
                f"(allowed: {allowed_growth})",
                file=sys.stderr,
            )
        return 1
    grew = f", growth {score_growth} within tolerance {allowed_growth}" if score_growth > 0 else ""
    print(
        f"Thinning ratchet: PASS ({current['hotspot_count']} hotspots, "
        f"weighted_score={current['weighted_score']}{grew})"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=DESCRIPTION)
    result.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = result.add_subparsers(dest="command", required=True)
    creating = sub.add_parser("baseline")
    creating.add_argument("--upstream", default="origin/main")
    creating.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "thinning_baseline.json",
    )
    creating.set_defaults(func=baseline)
    checking = sub.add_parser("check")
    checking.add_argument(
        "--baseline",
        type=Path,
        default=Path(__file__).resolve().parent / "thinning_baseline.json",
    )
    checking.add_argument(
        "--tolerance-pct",
        type=float,
        default=1.0,
        help="allowed weighted-score growth over baseline, as a percentage",
    )
    checking.set_defaults(func=check)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
