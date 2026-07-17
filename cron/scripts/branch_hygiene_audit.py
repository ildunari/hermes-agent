#!/usr/bin/env python3
"""Branch-hygiene audit for the Hermes core repo.

Read-only. Classifies every local branch against the live branch by PATCH
CONTENT (git cherry), not tip ancestry, so rebased/squashed work is not
misread as stranded (the failure that lost a fix for a week).

Output contract (for the nightly cron job):
  - Prints a compact JSON block on stdout for the agent to summarize.
  - Emits nothing actionable when there is no genuinely-stranded CODE work
    (the agent turns that into [SILENT]).

It NEVER mutates the repo. Deletion of already-merged branches and any
merge-to-live are the agent's job, gated by review + Kosta's approval.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

REPO = "/Users/Kosta/.hermes/hermes-agent"
LIVE = "local/studio-slim"
PROTECTED = {LIVE, "main"}
# Branches that are intentionally long-lived / not meant to auto-merge.
ARCHIVE_PREFIXES = ("archive/", "sandbox/", "backup/", "local/studio-customizations")
DOCS_PREFIXES = ("plan/", "review/", "research/", "repair/")
STALE_DAYS = 45  # code branches older than this are "cold" — report, don't push


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", REPO, *args],
        capture_output=True, text=True, check=False,
    ).stdout.strip()


def branch_age_days(branch: str) -> float:
    iso = git("log", "-1", "--format=%cI", branch)
    if not iso:
        return 1e9
    dt = datetime.fromisoformat(iso)
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400


def classify(branch: str) -> dict:
    cherry = git("cherry", LIVE, branch)
    lines = [l for l in cherry.splitlines() if l.strip()]
    stranded_shas = [l[2:] for l in lines if l.startswith("+")]
    applied = sum(1 for l in lines if l.startswith("-"))

    # Early bucketing that does NOT need the expensive per-commit file walk.
    if not stranded_shas:
        return {"branch": branch, "kind": "already_merged", "stranded_commits": 0,
                "already_applied": applied, "age_days": round(branch_age_days(branch), 1),
                "code_files": [], "subject": git("log", "-1", "--format=%s", branch)}
    if branch.startswith(ARCHIVE_PREFIXES):
        return {"branch": branch, "kind": "archive", "stranded_commits": len(stranded_shas),
                "already_applied": applied, "age_days": round(branch_age_days(branch), 1),
                "code_files": [], "subject": git("log", "-1", "--format=%s", branch)}

    # Files touched by the genuinely-unlanded commits only — one diff call, not
    # one per commit. Range LIVE..branch scoped to the stranded tips.
    changed_files: set[str] = set()
    diff_out = git("diff", "--name-only", f"{LIVE}...{branch}")
    for f in diff_out.splitlines():
        if f.strip():
            changed_files.add(f.strip())
    code_files = [
        f for f in changed_files
        if not (f.endswith(".md") or f.startswith("docs/") or f.startswith(".fable/"))
    ]

    if branch.startswith(DOCS_PREFIXES) or not code_files:
        kind = "docs_only"
    else:
        kind = "stranded_code"

    return {
        "branch": branch,
        "kind": kind,
        "stranded_commits": len(stranded_shas),
        "already_applied": applied,
        "age_days": round(branch_age_days(branch), 1),
        "code_files": sorted(code_files)[:12],
        "subject": git("log", "-1", "--format=%s", branch),
    }


def main() -> int:
    branches = [
        b for b in git("for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
        if b and b not in PROTECTED
    ]
    results = [classify(b) for b in branches]

    buckets: dict[str, list] = {
        "already_merged": [], "archive": [], "docs_only": [], "stranded_code": [],
    }
    for r in results:
        buckets[r["kind"]].append(r)

    hot_code = sorted(
        (r for r in buckets["stranded_code"] if r["age_days"] <= STALE_DAYS),
        key=lambda r: r["age_days"],
    )
    cold_code = sorted(
        (r for r in buckets["stranded_code"] if r["age_days"] > STALE_DAYS),
        key=lambda r: r["age_days"],
    )

    report = {
        "repo": REPO,
        "live_branch": LIVE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {k: len(v) for k, v in buckets.items()},
        "deletable_already_merged": sorted(r["branch"] for r in buckets["already_merged"]),
        "stranded_code_hot": hot_code,      # <= STALE_DAYS: propose merge
        "stranded_code_cold": [             # old: report for a decision only
            {"branch": r["branch"], "age_days": r["age_days"], "subject": r["subject"]}
            for r in cold_code
        ],
        "actionable": bool(hot_code) or bool(buckets["already_merged"]),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
