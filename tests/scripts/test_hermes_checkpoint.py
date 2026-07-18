from __future__ import annotations

import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hermes_checkpoint.py"


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT), "--repo", str(repo), *args],
        text=True,
        capture_output=True,
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def test_snapshot_preserves_head_index_and_restores_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    index = git(repo, "write-tree")
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    result = run(repo, "snapshot")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "COMPLETE"
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "write-tree") == index
    restored = tmp_path / "restored"
    restore = run(repo, "restore", payload["ref"], "--to-worktree", str(restored))
    assert restore.returncode == 0, restore.stderr
    assert (restored / "tracked.txt").read_text(encoding="utf-8") == "two\n"
    assert (restored / "new.txt").read_text(encoding="utf-8") == "new\n"


def test_secret_path_is_partial_and_not_in_snapshot(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    (repo / ".env").write_text("API_KEY=abcdefghijklmnop123456\n", encoding="utf-8")

    result = run(repo, "snapshot")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "PARTIAL"
    assert payload["omissions"][0]["path"] == ".env"
    tree = git(repo, "ls-tree", "-r", "--name-only", payload["commit"]).splitlines()
    assert "safe.txt" in tree
    assert ".env" not in tree
    assert "abcdefghijklmnop123456" not in result.stdout


def test_failed_when_every_change_is_omitted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / ".env").write_text("CLIENT_SECRET=abcdefghijklmnop123456\n", encoding="utf-8")

    result = run(repo, "snapshot")

    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "FAILED"
    assert not git(repo, "for-each-ref", "--format=%(refname)", "refs/hermes/checkpoints")


def test_oauth_json_and_neutral_token_file_are_omitted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    (repo / "auth.json").write_text(
        '{"refresh_token":"abcdefghijklmnop123456"}\n',
        encoding="utf-8",
    )
    (repo / "notes.txt").write_text(
        "api_key=abcdefghijklmnop123456\n",
        encoding="utf-8",
    )

    result = run(repo, "snapshot")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "PARTIAL"
    assert {item["path"] for item in payload["omissions"]} == {
        "auth.json",
        "notes.txt",
    }
    tree = git(repo, "ls-tree", "-r", "--name-only", payload["commit"]).splitlines()
    assert "safe.txt" in tree
    assert "auth.json" not in tree
    assert "notes.txt" not in tree
