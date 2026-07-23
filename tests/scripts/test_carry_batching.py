from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "carry.py"
SPEC = importlib.util.spec_from_file_location("carry_batching", SCRIPT)
assert SPEC and SPEC.loader
CARRY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CARRY
SPEC.loader.exec_module(CARRY)


def feature(feature_id: str, tests: tuple[str, ...]) -> object:
    return CARRY.Feature(
        id=feature_id,
        owner="test",
        classification="keep-core",
        checks=(),
        tests=tests,
        owner_paths=(),
        dependent_paths=(),
        collision_paths=(),
        consumer={"command": ["true"]},
        runtime_probe={"command": ["true"]},
        provisional_until=None,
    )


def registry(root: Path) -> object:
    return CARRY.Registry(
        root=root,
        manifest_path=root / "scripts" / "local_carry_manifest.yaml",
        upstream_ref="origin/main",
        managed_roots=("core",),
        checks=(),
        features=(),
        exemptions=(),
    )


def completed(args: list[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout="out", stderr=""
    )


def test_classify_feature_tests_splits_and_flags_unsupported() -> None:
    python, web, desktop, unsupported = CARRY.classify_feature_tests(
        (
            "tests/test_a.py",
            "web/src/a.test.ts",
            "apps/desktop/src/b.test.tsx",
            "docs/manual.md",
        )
    )

    assert python == ["tests/test_a.py"]
    assert web == ["src/a.test.ts"]
    assert desktop == ["src/b.test.tsx"]
    assert unsupported == ["docs/manual.md"]


def test_batched_verify_runs_at_most_three_commands(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_command(cwd, *args, check=True, env=None):
        calls.append((cwd, args))
        return completed(list(args))

    monkeypatch.setattr(CARRY, "command", fake_command)
    reg = registry(tmp_path)
    features = [
        feature("alpha", ("tests/test_a.py", "web/src/a.test.ts")),
        feature(
            "beta",
            ("tests/test_b.py", "tests/test_a.py", "apps/desktop/src/b.test.tsx"),
        ),
    ]

    results = CARRY.run_batched_tests(reg, features)

    assert len(calls) == 3
    python_call = calls[0][1]
    assert python_call[1] == "-j"
    assert int(python_call[2]) >= 2
    assert "tests/test_a.py" in python_call and "tests/test_b.py" in python_call
    assert python_call.count("tests/test_a.py") == 1
    assert calls[1] == (
        tmp_path / "web",
        ("npm", "exec", "--", "vitest", "run", "src/a.test.ts"),
    )
    assert calls[2] == (
        tmp_path / "apps" / "desktop",
        ("npm", "run", "test:ui", "--", "--run", "src/b.test.tsx"),
    )
    assert results["alpha"].returncode == 0
    assert results["beta"].returncode == 0


def test_batch_failure_falls_back_per_feature_for_attribution(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_command(cwd, *args, check=True, env=None):
        failing = "run_tests.sh" in args[0]
        return completed(list(args), returncode=1 if failing else 0)

    fallbacks: list[str] = []

    def fake_per_feature(reg, item):
        fallbacks.append(item.id)
        return completed(["per-feature"], returncode=1)

    monkeypatch.setattr(CARRY, "command", fake_command)
    monkeypatch.setattr(CARRY, "run_feature_tests", fake_per_feature)
    reg = registry(tmp_path)
    features = [
        feature("alpha", ("tests/test_a.py",)),
        feature("beta", ("web/src/b.test.ts",)),
    ]

    results = CARRY.run_batched_tests(reg, features)

    assert fallbacks == ["alpha"]
    assert results["alpha"].returncode == 1
    assert results["beta"].returncode == 0


def test_unsupported_paths_use_per_feature_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        CARRY,
        "command",
        lambda cwd, *args, check=True, env=None: completed(list(args)),
    )
    reg = registry(tmp_path)

    results = CARRY.run_batched_tests(reg, [feature("odd", ("docs/manual.md",))])

    assert results["odd"].returncode == 2
    assert "unsupported carry test paths" in results["odd"].stderr


def verify_args(**overrides) -> argparse.Namespace:
    values = {
        "root": Path("."),
        "manifest": None,
        "feature": None,
        "changed_since": None,
        "skip_runtime_probes": False,
        "probes_only": False,
        "json": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_probes_only_skips_behavior_tests(tmp_path: Path, monkeypatch) -> None:
    reg = CARRY.Registry(
        root=tmp_path,
        manifest_path=tmp_path / "manifest.yaml",
        upstream_ref="origin/main",
        managed_roots=("core",),
        checks=(),
        features=(feature("alpha", ("tests/test_a.py",)),),
        exemptions=(),
    )
    monkeypatch.setattr(CARRY, "load", lambda args: reg)
    monkeypatch.setattr(CARRY, "validate_checks", lambda registry: [])
    monkeypatch.setattr(CARRY, "run_consumer", lambda registry, item: (True, "ok"))
    monkeypatch.setattr(CARRY, "run_probe", lambda registry, item: (True, "ok"))
    monkeypatch.setattr(
        CARRY,
        "run_batched_tests",
        lambda registry, selected: pytest.fail("probes-only must not run tests"),
    )
    report_path = tmp_path / "report.json"

    result = CARRY.verify(verify_args(probes_only=True, json=report_path))

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    record = report["features"]["alpha"]
    assert record["tests"] == {"status": "SKIPPED", "returncode": 0, "output": ""}
    assert record["consumer"]["status"] == "PASS"
    assert record["runtime_probe"]["status"] == "PASS"


def test_verify_uses_batched_results(tmp_path: Path, monkeypatch) -> None:
    reg = CARRY.Registry(
        root=tmp_path,
        manifest_path=tmp_path / "manifest.yaml",
        upstream_ref="origin/main",
        managed_roots=("core",),
        checks=(),
        features=(
            feature("alpha", ("tests/test_a.py",)),
            feature("beta", ("tests/test_b.py",)),
        ),
        exemptions=(),
    )
    monkeypatch.setattr(CARRY, "load", lambda args: reg)
    monkeypatch.setattr(CARRY, "validate_checks", lambda registry: [])
    monkeypatch.setattr(CARRY, "run_consumer", lambda registry, item: (True, "ok"))
    monkeypatch.setattr(
        CARRY,
        "run_batched_tests",
        lambda registry, selected: {
            "alpha": completed(["batch"]),
            "beta": completed(["batch"], returncode=1),
        },
    )
    report_path = tmp_path / "report.json"

    result = CARRY.verify(
        verify_args(skip_runtime_probes=True, json=report_path)
    )

    assert result == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["features"]["alpha"]["tests"]["status"] == "PASS"
    assert report["features"]["beta"]["tests"]["status"] == "FAIL"
    assert "behavior tests failed: beta" in report["failures"]


def test_verify_parser_accepts_probes_only() -> None:
    parser = CARRY.build_parser()

    args = parser.parse_args(["verify", "--probes-only"])

    assert args.probes_only is True
    assert parser.parse_args(["verify"]).probes_only is False
