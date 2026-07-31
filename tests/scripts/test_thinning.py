from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "thinning.py"
SPEC = importlib.util.spec_from_file_location("thinning", SCRIPT)
assert SPEC and SPEC.loader
THINNING = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = THINNING
SPEC.loader.exec_module(THINNING)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_generated_baseline_does_not_score_itself(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "scripts" / "thinning_baseline.json").write_text("{}\n")
    (repo / "tracked.py").write_text("VALUE = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "upstream")

    (repo / "scripts" / "thinning_baseline.json").write_text('{"changed": true}\n')
    (repo / "tracked.py").write_text("VALUE = 2\n")
    git(repo, "commit", "-qam", "local")

    payload = THINNING.compute(repo, "upstream")
    paths = {item["path"] for item in payload["hotspots"]}

    assert "tracked.py" in paths
    assert "scripts/thinning_baseline.json" not in paths


def _make_carry_repo(tmp_path: Path) -> Path:
    """Repo with an upstream branch and one carried change on main."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "tracked.py").write_text("VALUE = 1\n")
    (repo / "other.py").write_text("OTHER = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "upstream")
    (repo / "tracked.py").write_text("VALUE = 2\n")
    git(repo, "commit", "-qam", "carry")
    return repo


def _write_baseline(repo: Path) -> Path:
    import argparse
    import json

    baseline_path = repo / "scripts" / "thinning_baseline.json"
    ns = argparse.Namespace(root=repo, upstream="upstream", output=baseline_path)
    assert THINNING.baseline(ns) == 0
    return baseline_path


def _check(repo: Path, baseline_path: Path, tolerance_pct: float = 1.0) -> int:
    import argparse

    ns = argparse.Namespace(
        root=repo, baseline=baseline_path, tolerance_pct=tolerance_pct
    )
    return THINNING.check(ns)


def test_check_is_immune_to_upstream_movement(tmp_path: Path) -> None:
    repo = _make_carry_repo(tmp_path)
    baseline_path = _write_baseline(repo)
    assert _check(repo, baseline_path) == 0

    # Upstream advances with churn on the carried file. The pinned
    # upstream_sha must keep the score identical, so check still passes.
    git(repo, "checkout", "-q", "upstream")
    for i in range(5):
        (repo / "tracked.py").write_text(f"VALUE = 1  # upstream {i}\n")
        git(repo, "commit", "-qam", f"upstream churn {i}")
    git(repo, "checkout", "-q", "main")

    assert _check(repo, baseline_path) == 0


def test_check_tolerance_band(tmp_path: Path) -> None:
    repo = _make_carry_repo(tmp_path)
    baseline_path = _write_baseline(repo)

    # Grow the carry on the existing hotspot: 2 -> 4 changed lines,
    # i.e. +100% weighted score.
    (repo / "tracked.py").write_text("VALUE = 3\nEXTRA = 1\n")
    git(repo, "commit", "-qam", "carry growth")

    assert _check(repo, baseline_path, tolerance_pct=1.0) == 1
    assert _check(repo, baseline_path, tolerance_pct=150.0) == 0


def test_check_refuses_baseline_without_upstream_sha(tmp_path: Path) -> None:
    import json

    repo = _make_carry_repo(tmp_path)
    baseline_path = _write_baseline(repo)
    data = json.loads(baseline_path.read_text())
    del data["upstream_sha"]
    baseline_path.write_text(json.dumps(data))

    assert _check(repo, baseline_path) == 1


def test_check_new_hotspot_always_fails(tmp_path: Path) -> None:
    repo = _make_carry_repo(tmp_path)
    baseline_path = _write_baseline(repo)

    (repo / "other.py").write_text("OTHER = 2\n")
    git(repo, "commit", "-qam", "new carry file")

    assert _check(repo, baseline_path, tolerance_pct=1000.0) == 1


def test_check_reports_stale_baseline_after_pinned_upstream_is_merged(
    tmp_path: Path, capsys
) -> None:
    repo = _make_carry_repo(tmp_path)
    git(repo, "checkout", "-q", "upstream")
    (repo / "other.py").write_text("OTHER = 2\n")
    git(repo, "commit", "-qam", "upstream change")
    git(repo, "checkout", "-q", "main")
    baseline_path = _write_baseline(repo)

    git(repo, "merge", "--no-edit", "upstream")

    assert _check(repo, baseline_path) == 1
    assert "baseline is stale after an upstream merge" in capsys.readouterr().err
