"""Carry coverage must treat a *deleted* managed path as de-carry, not debt.

`carry.py validate` answers one question: "is every locally-carried path under
a managed root owned by a manifest feature, a legacy check, or an exemption?"

Deleting a carried file is the successful outcome of that question, not a
violation of it. Before this fix, `changed_paths` unioned the committed diff
with `git diff --cached --name-only`, which lists staged deletions as plain
paths. A de-carry commit that removed carried files therefore reported every
removed file as an "unowned managed path", so the tool actively failed the
work it exists to encourage — and the only way to get a green run was to keep
a dead exemption for a file that no longer exists.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import carry  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tiny repo with one managed root and an 'upstream' to diff against."""
    root = tmp_path / "repo"
    (root / "gateway").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")

    (root / "gateway" / "kept.py").write_text("x = 1\n")
    (root / "gateway" / "carried.py").write_text("y = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "branch", "upstream")
    return root


def _registry(root: Path) -> carry.Registry:
    return carry.Registry(
        root=root,
        manifest_path=root / "scripts" / "local_carry_manifest.yaml",
        upstream_ref="upstream",
        managed_roots=("gateway",),
        checks=(),
        features=(),
        exemptions=(),
    )


def test_staged_deletion_is_not_reported_as_unowned_carry(repo: Path) -> None:
    """Removing a carried file is de-carry; coverage must stay clean."""
    _git(repo, "rm", "-q", "gateway/carried.py")

    result = carry.coverage(_registry(repo), "upstream")

    assert "gateway/carried.py" not in result["uncovered"], (
        "a staged deletion was counted as unowned carry, which fails the very "
        "de-carry the manifest exists to drive"
    )
    assert result["uncovered"] == []


def test_committed_deletion_is_not_reported_as_unowned_carry(repo: Path) -> None:
    """Same property once the de-carry is committed, not just staged."""
    _git(repo, "rm", "-q", "gateway/carried.py")
    _git(repo, "commit", "-qm", "de-carry: drop carried.py")

    result = carry.coverage(_registry(repo), "upstream")

    assert result["uncovered"] == []


def test_added_unowned_managed_path_is_still_reported(repo: Path) -> None:
    """The guard must keep failing for genuinely new, unowned carry."""
    (repo / "gateway" / "new_carry.py").write_text("z = 3\n")
    _git(repo, "add", "-A")

    result = carry.coverage(_registry(repo), "upstream")

    assert result["uncovered"] == ["gateway/new_carry.py"]


def test_modified_unowned_managed_path_is_still_reported(repo: Path) -> None:
    """A modification is still live carry and must still be attributed."""
    (repo / "gateway" / "carried.py").write_text("y = 99\n")
    _git(repo, "add", "-A")

    result = carry.coverage(_registry(repo), "upstream")

    assert result["uncovered"] == ["gateway/carried.py"]
