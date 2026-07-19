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
