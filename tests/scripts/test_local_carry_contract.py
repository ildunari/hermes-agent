from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import check_local_carry_contract as carry


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def carry_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Carry Test")
    _git(repo, "config", "user.email", "carry@example.invalid")
    (repo / "existing.py").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "existing.py")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "existing.py").write_text("new\n", encoding="utf-8")
    (repo / "added.py").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "carry")
    return repo, base


def _manifest(path: Path, base: str, *, paths: list[str], limits: dict) -> Path:
    evidence = {
        "registration": "test registration",
        "runtime_caller": "test caller",
        "interface_test": "test contract",
        "artifact": "test artifact",
    }
    data = {
        "version": 1,
        "base_ref": base,
        "limits": limits,
        "paths": [
            {
                "path": item,
                "disposition": "must-carry",
                "owner": "test",
                "evidence": evidence,
            }
            for item in paths
        ],
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_validate_measures_new_and_modified_paths(carry_repo, tmp_path):
    repo, base = carry_repo
    manifest = _manifest(
        tmp_path / "manifest.yaml",
        base,
        paths=["added.py", "existing.py"],
        limits={"paths": 2, "total_changed_lines": 4, "modified_upstream_lines": 2},
    )

    metrics = carry.validate(repo, manifest)

    assert metrics.paths == 2
    assert metrics.total_changed_lines == 4
    assert metrics.modified_upstream_lines == 2


def test_validate_rejects_unknown_path(carry_repo, tmp_path):
    repo, base = carry_repo
    manifest = _manifest(
        tmp_path / "manifest.yaml",
        base,
        paths=["existing.py"],
        limits={"paths": 2, "total_changed_lines": 4, "modified_upstream_lines": 2},
    )

    with pytest.raises(carry.CarryContractError, match="unclassified carry paths: added.py"):
        carry.validate(repo, manifest)


def test_validate_rejects_metric_regression(carry_repo, tmp_path):
    repo, base = carry_repo
    manifest = _manifest(
        tmp_path / "manifest.yaml",
        base,
        paths=["added.py", "existing.py"],
        limits={"paths": 2, "total_changed_lines": 3, "modified_upstream_lines": 2},
    )

    with pytest.raises(carry.CarryContractError, match="total_changed_lines regressed"):
        carry.validate(repo, manifest)


def test_validate_rejects_incomplete_evidence(carry_repo, tmp_path):
    repo, base = carry_repo
    manifest = _manifest(
        tmp_path / "manifest.yaml",
        base,
        paths=["added.py", "existing.py"],
        limits={"paths": 2, "total_changed_lines": 4, "modified_upstream_lines": 2},
    )
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["paths"][0]["evidence"]["artifact"] = ""
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(carry.CarryContractError, match="missing evidence"):
        carry.validate(repo, manifest)


def test_validate_support_gate_requires_every_declared_file(carry_repo, tmp_path):
    repo, base = carry_repo
    manifest = _manifest(
        tmp_path / "manifest.yaml",
        base,
        paths=["added.py", "existing.py"],
        limits={"paths": 2, "total_changed_lines": 4, "modified_upstream_lines": 2},
    )
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["required_support"] = ["support/required.py"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    support_root = tmp_path / "plugins"

    with pytest.raises(carry.CarryContractError, match="missing required support"):
        carry.validate(repo, manifest, support_root=support_root)

    required = support_root / "support" / "required.py"
    required.parent.mkdir(parents=True)
    required.write_text("ready = True\n", encoding="utf-8")

    assert carry.validate(repo, manifest, support_root=support_root).paths == 2
