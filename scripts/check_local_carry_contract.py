#!/usr/bin/env python3
"""Fail fast when a smart update drops known local carry invariants."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Check:
    id: str
    path: str
    needles: tuple[str, ...]
    forbidden_needles: tuple[str, ...]
    description: str


FORBIDDEN_CHECKS = (
    ("run_agent.py", "100.93.10.54", "machine address must not own provider behavior"),
    ("plugins/model-providers/custom/__init__.py", "100.93.10.54", "custom provider behavior must follow model identity"),
)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")

    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")

    return value


def _strings(value: Any, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "a non-empty" if not allow_empty else "a"
        raise ValueError(f"{label} must be {qualifier} list of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{label} must contain only non-empty strings")

    return tuple(value)


def _check(value: Any, label: str) -> Check:
    item = _mapping(value, label)

    return Check(
        id=_string(item.get("id"), f"{label}.id"),
        path=_string(item.get("path"), f"{label}.path"),
        needles=_strings(item.get("needles"), f"{label}.needles"),
        forbidden_needles=_strings(
            item.get("forbidden_needles", []),
            f"{label}.forbidden_needles",
        ),
        description=_string(item.get("description"), f"{label}.description"),
    )


def load_manifest(path: Path) -> tuple[list[Check], dict[str, tuple[str, ...]]]:
    raw = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "manifest")
    if raw.get("version") != 1:
        raise ValueError("manifest.version must be 1")

    checks_raw = raw.get("checks")
    features_raw = raw.get("features")
    if not isinstance(checks_raw, list):
        raise ValueError("manifest.checks must be a list")
    if not isinstance(features_raw, list):
        raise ValueError("manifest.features must be a list")

    checks: list[Check] = []
    check_ids: set[str] = set()
    feature_tests: dict[str, tuple[str, ...]] = {}

    def append_check(value: Any, label: str) -> None:
        check = _check(value, label)
        if check.id in check_ids:
            raise ValueError(f"duplicate check id: {check.id}")
        check_ids.add(check.id)
        checks.append(check)

    for index, value in enumerate(checks_raw):
        append_check(value, f"checks[{index}]")

    for index, value in enumerate(features_raw):
        label = f"features[{index}]"
        feature = _mapping(value, label)
        feature_id = _string(feature.get("id"), f"{label}.id")
        if feature_id in feature_tests:
            raise ValueError(f"duplicate feature id: {feature_id}")
        feature_tests[feature_id] = _strings(feature.get("tests"), f"{label}.tests", allow_empty=False)

        feature_checks = feature.get("checks")
        if not isinstance(feature_checks, list) or not feature_checks:
            raise ValueError(f"{label}.checks must be a non-empty list")
        for check_index, item in enumerate(feature_checks):
            append_check(item, f"{label}.checks[{check_index}]")

    return checks, feature_tests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest_path = (args.manifest or root / "scripts" / "local_carry_manifest.yaml").resolve()
    failures: list[str] = []

    try:
        checks, feature_tests = load_manifest(manifest_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Local carry contract: FAIL\n- INVALID MANIFEST: {manifest_path}: {exc}", file=sys.stderr)
        return 1

    for check in checks:
        path = root / check.path
        if not path.is_file():
            failures.append(f"MISSING FILE: {check.path} [{check.id}] ({check.description})")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in check.needles:
            if needle.lower() not in text.lower():
                failures.append(f"MISSING SYMBOL: {check.path}: {needle!r} [{check.id}] ({check.description})")
        for needle in check.forbidden_needles:
            if needle.lower() in text.lower():
                failures.append(
                    f"FORBIDDEN SYMBOL: {check.path}: {needle!r} "
                    f"[{check.id}] ({check.description})"
                )

    for feature_id, tests in feature_tests.items():
        for test in tests:
            if not (root / test).is_file():
                failures.append(f"MISSING TEST: {test} ({feature_id})")

    for rel_path, forbidden, description in FORBIDDEN_CHECKS:
        path = root / rel_path
        if path.is_file() and forbidden in path.read_text(encoding="utf-8", errors="replace"):
            failures.append(f"FORBIDDEN SYMBOL: {rel_path}: {forbidden!r} ({description})")

    if failures:
        print("Local carry contract: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(
        f"Local carry contract: PASS ({len(checks)} connected surfaces, "
        f"{len(feature_tests)} documented features checked)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
