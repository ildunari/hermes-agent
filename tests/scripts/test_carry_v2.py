from __future__ import annotations

import subprocess
import json
from pathlib import Path

import yaml


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "carry.py"
GATE = Path(__file__).resolve().parents[2] / "scripts" / "carry_gate.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT), "--root", str(repo), *args],
        text=True,
        capture_output=True,
    )


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "core").mkdir()
    (repo / "scripts").mkdir()
    (repo / "core" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "test_feature.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "upstream"], cwd=repo, check=True)
    (repo / "core" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "carry"], cwd=repo, check=True)
    return repo


def manifest(repo: Path, *, owner_paths: list[str] | None = None) -> None:
    payload = {
        "schema_version": 2,
        "upstream_ref": "upstream",
        "managed_roots": ["core"],
        "exemptions_file": "scripts/exemptions.yaml",
        "checks": [],
        "features": [
            {
                "id": "feature",
                "owner": "test",
                "class": "keep-core",
                "owner_paths": owner_paths or ["core/feature.py"],
                "tests": ["test_feature.py"],
                "checks": [
                    {
                        "id": "value",
                        "path": "core/feature.py",
                        "needles": ["VALUE = 2"],
                        "description": "value",
                    }
                ],
                "consumer": {"command": ["python3", "-c", "import pathlib; assert pathlib.Path('core/feature.py').is_file()"]},
                "runtime_probe": {"command": ["python3", "-c", "print('ok')"]},
            }
        ],
    }
    (repo / "scripts" / "local_carry_manifest.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def test_validate_requires_complete_ownership(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    manifest(repo, owner_paths=["core/other.py"])

    result = run(repo, "validate")

    assert result.returncode == 1
    assert "unowned managed path: core/feature.py" in result.stderr


def test_validate_passes_at_full_coverage(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    manifest(repo)

    result = run(repo, "validate")

    assert result.returncode == 0, result.stderr
    assert "1/1 managed paths" in result.stdout


def test_primary_owner_collision_fails(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    manifest(repo)
    path = repo / "scripts" / "local_carry_manifest.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    duplicate = dict(payload["features"][0])
    duplicate["id"] = "duplicate"
    duplicate["checks"] = [dict(duplicate["checks"][0], id="duplicate-value")]
    payload["features"].append(duplicate)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    result = run(repo, "validate")

    assert result.returncode == 1
    assert "primary owner collision" in result.stderr


def test_active_automation_uses_slim_branch() -> None:
    wrapper = Path("/Users/Kosta/.hermes/scripts/checkpoint_hermes_agent.py")
    jobs = Path("/Users/Kosta/.hermes/profiles/gpt/cron/jobs.json")
    assert "local/studio-customizations" not in wrapper.read_text(encoding="utf-8")
    active = [
        job
        for job in json.loads(jobs.read_text(encoding="utf-8"))["jobs"]
        if job.get("enabled")
    ]
    stale = [
        str(job.get("name"))
        for job in active
        if "local/studio-customizations" in json.dumps(job)
    ]
    assert stale == []


def test_staged_gate_uses_index_bytes_not_working_tree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    manifest(repo)
    (repo / "scripts" / "carry.py").write_bytes(SCRIPT.read_bytes())
    (repo / "scripts" / "carry_gate.py").write_bytes(GATE.read_bytes())
    subprocess.run(["git", "add", "scripts"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "install carry gate"], cwd=repo, check=True)

    feature = repo / "core" / "feature.py"
    feature.write_text("VALUE = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", str(feature)], cwd=repo, check=True)
    feature.write_text("VALUE = 2\n", encoding="utf-8")

    result = subprocess.run(
        ["python3", str(GATE), "--root", str(repo), "staged"],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "missing needle" in result.stderr


def test_desktop_carry_tests_use_the_hardened_ui_script() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '["npm", "run", "test:ui", "--", "--run", *desktop_tests]' in source
