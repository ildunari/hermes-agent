#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DESCRIPTION = "Validate and verify the connected local/studio-slim carry registry."
TERMINAL_LEDGER_STATES = {"completed", "failed", "aborted"}


@dataclass(frozen=True)
class Check:
    id: str
    path: str
    needles: tuple[str, ...]
    forbidden_needles: tuple[str, ...]


@dataclass(frozen=True)
class Feature:
    id: str
    owner: str
    classification: str
    checks: tuple[Check, ...]
    tests: tuple[str, ...]
    owner_paths: tuple[str, ...]
    dependent_paths: tuple[str, ...]
    collision_paths: tuple[str, ...]
    consumer: dict[str, Any] | None
    runtime_probe: dict[str, Any] | None
    provisional_until: dt.date | None

    @property
    def all_paths(self) -> set[str]:
        return {
            *self.owner_paths,
            *self.dependent_paths,
            *self.collision_paths,
            *self.tests,
            *(check.path for check in self.checks),
        }


@dataclass(frozen=True)
class Exemption:
    path_glob: str
    reason: str
    expires: dt.date


@dataclass
class Registry:
    root: Path
    manifest_path: Path
    upstream_ref: str
    managed_roots: tuple[str, ...]
    checks: tuple[Check, ...]
    features: tuple[Feature, ...]
    exemptions: tuple[Exemption, ...]


def command(
    root: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def git(root: Path, *args: str) -> str:
    return command(root, "git", *args).stdout.strip()


def mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def strings(value: Any, label: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value):
        raise ValueError(f"{label} must be {'a non-empty ' if required else 'a '}list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{label} must contain non-empty strings")
    return tuple(item.strip() for item in value)


def date_value(value: Any, label: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(text(value, label))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def parse_check(value: Any, label: str) -> Check:
    item = mapping(value, label)
    return Check(
        id=text(item.get("id"), f"{label}.id"),
        path=text(item.get("path"), f"{label}.path"),
        needles=strings(item.get("needles", []), f"{label}.needles"),
        forbidden_needles=strings(
            item.get("forbidden_needles", []),
            f"{label}.forbidden_needles",
        ),
    )


def load_registry(root: Path, manifest_path: Path) -> Registry:
    raw = mapping(yaml.safe_load(manifest_path.read_text(encoding="utf-8")), "manifest")
    if raw.get("schema_version") != 2:
        raise ValueError("manifest.schema_version must be 2")
    upstream_ref = text(raw.get("upstream_ref", "origin/main"), "manifest.upstream_ref")
    managed_roots = strings(raw.get("managed_roots"), "manifest.managed_roots", required=True)
    global_checks = tuple(
        parse_check(value, f"checks[{index}]")
        for index, value in enumerate(raw.get("checks", []))
    )
    default_provisional = raw.get("legacy_provisional_until")
    features: list[Feature] = []
    feature_ids: set[str] = set()
    check_ids = {item.id for item in global_checks}
    primary_owners: dict[str, str] = {}
    for index, value in enumerate(raw.get("features", [])):
        label = f"features[{index}]"
        item = mapping(value, label)
        feature_id = text(item.get("id"), f"{label}.id")
        if feature_id in feature_ids:
            raise ValueError(f"duplicate feature id: {feature_id}")
        feature_ids.add(feature_id)
        checks = tuple(
            parse_check(check, f"{label}.checks[{check_index}]")
            for check_index, check in enumerate(item.get("checks", []))
        )
        for feature_check in checks:
            if feature_check.id in check_ids:
                raise ValueError(f"duplicate check id: {feature_check.id}")
            check_ids.add(feature_check.id)
        owner_paths = strings(item.get("owner_paths", []), f"{label}.owner_paths")
        if not owner_paths:
            owner_paths = tuple(check.path for check in checks)
        for owned in owner_paths:
            prior = primary_owners.get(owned)
            if prior:
                raise ValueError(
                    f"primary owner collision for {owned}: {prior} and {feature_id}"
                )
            primary_owners[owned] = feature_id
        provisional_raw = item.get("provisional_until", default_provisional)
        provisional = (
            date_value(provisional_raw, f"{label}.provisional_until")
            if provisional_raw
            else None
        )
        classification = text(
            item.get("class", "keep-core"),
            f"{label}.class",
        )
        if classification not in {
            "keep-core",
            "externalize",
            "upstream-replaced",
            "retire",
        }:
            raise ValueError(f"{label}.class is invalid: {classification}")
        features.append(
            Feature(
                id=feature_id,
                owner=text(item.get("owner", "kosta"), f"{label}.owner"),
                classification=classification,
                checks=checks,
                tests=strings(item.get("tests"), f"{label}.tests", required=True),
                owner_paths=owner_paths,
                dependent_paths=strings(
                    item.get("dependent_paths", []),
                    f"{label}.dependent_paths",
                ),
                collision_paths=strings(
                    item.get("collision_paths", []),
                    f"{label}.collision_paths",
                ),
                consumer=item.get("consumer"),
                runtime_probe=item.get("runtime_probe"),
                provisional_until=provisional,
            )
        )
    exemptions_setting = text(
        raw.get("exemptions_file", "scripts/local_carry_exemptions.yaml"),
        "manifest.exemptions_file",
    )
    exemptions_path = root / exemptions_setting
    if manifest_path.parent != root / "scripts":
        exemptions_path = manifest_path.parent / Path(exemptions_setting).name
    exemptions: list[Exemption] = []
    if exemptions_path.is_file():
        exemption_raw = yaml.safe_load(exemptions_path.read_text(encoding="utf-8")) or {}
        for index, value in enumerate(mapping(exemption_raw, "exemptions").get("exemptions", [])):
            label = f"exemptions[{index}]"
            item = mapping(value, label)
            exemptions.append(
                Exemption(
                    path_glob=text(item.get("path_glob"), f"{label}.path_glob"),
                    reason=text(item.get("reason"), f"{label}.reason"),
                    expires=date_value(item.get("expires"), f"{label}.expires"),
                )
            )
    return Registry(
        root=root,
        manifest_path=manifest_path,
        upstream_ref=upstream_ref,
        managed_roots=managed_roots,
        checks=global_checks,
        features=tuple(features),
        exemptions=tuple(exemptions),
    )


def is_managed(registry: Registry, path: str) -> bool:
    return any(path == root or path.startswith(f"{root.rstrip('/')}/") for root in registry.managed_roots)


def changed_paths(registry: Registry, base: str | None = None) -> tuple[str, list[str]]:
    upstream = base or registry.upstream_ref
    merge_base = git(registry.root, "merge-base", "HEAD", upstream)
    paths = git(
        registry.root,
        "diff",
        "--name-only",
        f"{merge_base}...HEAD",
    ).splitlines()
    staged = git(registry.root, "diff", "--cached", "--name-only").splitlines()
    return merge_base, sorted(
        path for path in set(paths + staged) if is_managed(registry, path)
    )


def path_matches(pattern: str, path: str) -> bool:
    return pattern == path or fnmatch.fnmatch(path, pattern)


def owner_for(registry: Registry, path: str) -> list[str]:
    owners: list[str] = []
    for check in registry.checks:
        if path_matches(check.path, path):
            owners.append(f"legacy:{check.id}")
    for feature in registry.features:
        if any(path_matches(pattern, path) for pattern in feature.owner_paths):
            owners.append(feature.id)
    return sorted(set(owners))


def exemption_for(registry: Registry, path: str) -> Exemption | None:
    today = dt.date.today()
    for exemption in registry.exemptions:
        if exemption.expires >= today and path_matches(exemption.path_glob, path):
            return exemption
    return None


def needle_relocations(registry: Registry, needle: str, original_path: str) -> list[str]:
    """Find tracked files containing a moved sentinel after an upstream refactor."""
    result = command(
        registry.root,
        "git",
        "grep",
        "-I",
        "-l",
        "-i",
        "-F",
        "-e",
        needle,
        "--",
        check=False,
    )
    if result.returncode not in {0, 1}:
        return []
    return [
        path
        for path in sorted(set(result.stdout.splitlines()))
        if path != original_path
    ][:5]


def validate_checks(registry: Registry) -> list[str]:
    failures: list[str] = []
    seen: set[str] = set()
    for check in (*registry.checks, *(check for feature in registry.features for check in feature.checks)):
        if check.id in seen:
            failures.append(f"duplicate check id: {check.id}")
        seen.add(check.id)
        path = registry.root / check.path
        if not path.is_file():
            failures.append(f"missing implementation: {check.path} [{check.id}]")
            continue
        content = path.read_text(encoding="utf-8", errors="replace").lower()
        for needle in check.needles:
            if needle.lower() not in content:
                message = f"missing needle {needle!r}: {check.path} [{check.id}]"
                relocations = needle_relocations(registry, needle, check.path)
                if relocations:
                    message += "; possible relocation: " + ", ".join(relocations)
                failures.append(message)
        for needle in check.forbidden_needles:
            if needle.lower() in content:
                failures.append(f"forbidden needle {needle!r}: {check.path} [{check.id}]")
    for feature in registry.features:
        for test in feature.tests:
            if not (registry.root / test).is_file():
                failures.append(f"missing test: {test} [{feature.id}]")
        if feature.provisional_until and feature.provisional_until < dt.date.today():
            if feature.consumer is None or feature.runtime_probe is None:
                failures.append(f"expired provisional feature is disconnected: {feature.id}")
        elif feature.provisional_until is None:
            if feature.consumer is None:
                failures.append(f"missing runtime consumer: {feature.id}")
            if feature.runtime_probe is None:
                failures.append(f"missing runtime probe: {feature.id}")
    for exemption in registry.exemptions:
        if exemption.expires < dt.date.today():
            failures.append(
                f"expired exemption: {exemption.path_glob} ({exemption.expires.isoformat()})"
            )
    return failures


def coverage(registry: Registry, base: str | None = None) -> dict[str, Any]:
    merge_base, relevant = changed_paths(registry, base)
    uncovered: list[str] = []
    mapped = 0
    exempted = 0
    for path in relevant:
        if owner_for(registry, path):
            mapped += 1
        elif exemption_for(registry, path):
            exempted += 1
        else:
            uncovered.append(path)
    total = len(relevant)
    return {
        "merge_base": merge_base,
        "upstream": base or registry.upstream_ref,
        "total": total,
        "mapped": mapped,
        "exempted": exempted,
        "covered": mapped + exempted,
        "percent": 100.0 if total == 0 else round((mapped + exempted) * 100 / total, 2),
        "uncovered": uncovered,
    }


def load(args: argparse.Namespace) -> Registry:
    root = args.root.resolve()
    manifest = (args.manifest or root / "scripts" / "local_carry_manifest.yaml").resolve()
    return load_registry(root, manifest)


def validate(args: argparse.Namespace) -> int:
    try:
        registry = load(args)
        failures = validate_checks(registry)
        result = coverage(registry, args.against)
        failures.extend(f"unowned managed path: {path}" for path in result["uncovered"])
    except (OSError, ValueError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        print(f"Carry registry: FAIL\n- {exc}", file=sys.stderr)
        return 1
    if failures:
        print("Carry registry: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(
        "Carry registry: PASS "
        f"({result['covered']}/{result['total']} managed paths, "
        f"{len(registry.features)} features, {len(registry.checks)} legacy surfaces)"
    )
    return 0


def impacted_features(registry: Registry, changed_since: str | None) -> list[Feature]:
    if not changed_since:
        return list(registry.features)
    changed = set(
        git(registry.root, "diff", "--name-only", f"{changed_since}...HEAD").splitlines()
    )
    return [
        feature
        for feature in registry.features
        if any(any(path_matches(pattern, path) for pattern in feature.all_paths) for path in changed)
    ]


def run_consumer(registry: Registry, feature: Feature) -> tuple[bool, str]:
    if feature.consumer is None:
        return False, "provisional consumer"
    consumer = mapping(feature.consumer, f"{feature.id}.consumer")
    command_args = strings(consumer.get("command"), f"{feature.id}.consumer.command", required=True)
    result = command(registry.root, *command_args, check=False)
    return result.returncode == 0, result.stderr.strip() or result.stdout.strip()


def run_probe(registry: Registry, feature: Feature) -> tuple[bool, str]:
    if feature.runtime_probe is None:
        return False, "provisional runtime probe"
    probe = mapping(feature.runtime_probe, f"{feature.id}.runtime_probe")
    command_args = strings(probe.get("command"), f"{feature.id}.runtime_probe.command", required=True)
    result = command(registry.root, *command_args, check=False)
    return result.returncode == 0, result.stderr.strip() or result.stdout.strip()


def classify_feature_tests(
    tests: tuple[str, ...],
) -> tuple[list[str], list[str], list[str], list[str]]:
    python_tests = [path for path in tests if Path(path).suffix == ".py"]
    web_tests = [
        str(Path(path).relative_to("web"))
        for path in tests
        if path.startswith("web/") and Path(path).suffix in {".ts", ".tsx"}
    ]
    desktop_tests = [
        str(Path(path).relative_to("apps/desktop"))
        for path in tests
        # .mjs: vitest runs script-side .test.mjs files the same as .test.ts
        if path.startswith("apps/desktop/") and Path(path).suffix in {".ts", ".tsx", ".mjs"}
    ]
    unsupported = sorted(
        set(tests)
        - {
            *python_tests,
            *(f"web/{path}" for path in web_tests),
            *(f"apps/desktop/{path}" for path in desktop_tests),
        }
    )
    return python_tests, web_tests, desktop_tests, unsupported


def run_feature_tests(
    registry: Registry,
    feature: Feature,
) -> subprocess.CompletedProcess[str]:
    python_tests, web_tests, desktop_tests, unsupported = classify_feature_tests(
        feature.tests
    )
    if unsupported:
        return subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr=f"unsupported carry test paths: {', '.join(unsupported)}",
        )
    commands: list[tuple[Path, list[str]]] = []
    if python_tests:
        commands.append(
            (
                registry.root,
                [
                    str(registry.root / "scripts" / "run_tests.sh"),
                    "-j",
                    "1",
                    *python_tests,
                    "-q",
                ],
            )
        )
    if web_tests:
        commands.append(
            (
                registry.root / "web",
                ["npm", "exec", "--", "vitest", "run", *web_tests],
            )
        )
    if desktop_tests:
        commands.append(
            (
                registry.root / "apps" / "desktop",
                ["npm", "run", "test:ui", "--", "--run", *desktop_tests],
            )
        )
    output: list[str] = []
    returncode = 0
    executed: list[str] = []
    for cwd, args in commands:
        result = command(cwd, *args, check=False)
        executed.extend(args)
        output.extend((result.stdout, result.stderr))
        if result.returncode:
            returncode = result.returncode
            break
    return subprocess.CompletedProcess(
        args=executed,
        returncode=returncode,
        stdout="".join(output),
        stderr="",
    )


def run_batched_tests(
    registry: Registry,
    features: list[Feature],
) -> dict[str, subprocess.CompletedProcess[str]]:
    """Run every selected feature's tests in at most three shared commands.

    Per-feature runs cost up to three cold subprocess groups each; the union
    runs once per runner instead. A failed batch falls back to per-feature
    runs only for the features whose tests were in it, for attribution.
    """
    classified = {
        feature.id: classify_feature_tests(feature.tests) for feature in features
    }
    python_union = sorted({test for py, _, _, _ in classified.values() for test in py})
    web_union = sorted({test for _, web, _, _ in classified.values() for test in web})
    desktop_union = sorted(
        {test for _, _, desktop, _ in classified.values() for test in desktop}
    )
    batches: dict[str, subprocess.CompletedProcess[str] | None] = {
        "python": None,
        "web": None,
        "desktop": None,
    }
    if python_union:
        batches["python"] = command(
            registry.root,
            str(registry.root / "scripts" / "run_tests.sh"),
            "-j",
            str(max(2, (os.cpu_count() or 2) // 2)),
            *python_union,
            "-q",
            check=False,
        )
    if web_union:
        batches["web"] = command(
            registry.root / "web",
            "npm",
            "exec",
            "--",
            "vitest",
            "run",
            *web_union,
            check=False,
        )
    if desktop_union:
        batches["desktop"] = command(
            registry.root / "apps" / "desktop",
            "npm",
            "run",
            "test:ui",
            "--",
            "--run",
            *desktop_union,
            check=False,
        )
    results: dict[str, subprocess.CompletedProcess[str]] = {}
    for feature in features:
        python_tests, web_tests, desktop_tests, unsupported = classified[feature.id]
        used = [
            batch
            for tests, batch in (
                (python_tests, batches["python"]),
                (web_tests, batches["web"]),
                (desktop_tests, batches["desktop"]),
            )
            if tests and batch is not None
        ]
        if unsupported or any(batch.returncode for batch in used):
            results[feature.id] = run_feature_tests(registry, feature)
            continue
        results[feature.id] = subprocess.CompletedProcess(
            args=[arg for batch in used for arg in batch.args],
            returncode=0,
            stdout="".join(batch.stdout + batch.stderr for batch in used),
            stderr="",
        )
    return results


def verify(args: argparse.Namespace) -> int:
    registry = load(args)
    failures = validate_checks(registry)
    selected = impacted_features(registry, args.changed_since)
    if args.feature:
        wanted = set(args.feature)
        selected = [feature for feature in selected if feature.id in wanted]
        missing = wanted.difference(feature.id for feature in selected)
        failures.extend(f"unknown or unaffected feature: {feature}" for feature in sorted(missing))
    evidence: dict[str, Any] = {"features": {}, "failures": failures}
    test_results: dict[str, subprocess.CompletedProcess[str]] = {}
    if not args.probes_only:
        test_results = run_batched_tests(registry, selected)
    for feature in selected:
        record: dict[str, Any] = {}
        provisional = bool(
            feature.provisional_until and feature.provisional_until >= dt.date.today()
        )
        consumer_ok, consumer_output = run_consumer(registry, feature)
        record["consumer"] = {
            "status": "PROVISIONAL" if provisional and not consumer_ok else ("PASS" if consumer_ok else "FAIL"),
            "output": consumer_output[-1000:],
        }
        if args.probes_only:
            record["tests"] = {"status": "SKIPPED", "returncode": 0, "output": ""}
        else:
            test_result = test_results[feature.id]
            record["tests"] = {
                "status": "PASS" if test_result.returncode == 0 else "FAIL",
                "returncode": test_result.returncode,
                "output": (test_result.stdout + test_result.stderr)[-3000:],
            }
            if test_result.returncode:
                failures.append(f"behavior tests failed: {feature.id}")
        if not consumer_ok and not provisional:
            failures.append(f"runtime consumer failed: {feature.id}")
        if args.skip_runtime_probes:
            record["runtime_probe"] = {"status": "SKIPPED"}
        else:
            probe_ok, probe_output = run_probe(registry, feature)
            record["runtime_probe"] = {
                "status": "PROVISIONAL" if provisional and not probe_ok else ("PASS" if probe_ok else "FAIL"),
                "output": probe_output[-1000:],
            }
            if not probe_ok and not provisional:
                failures.append(f"runtime probe failed: {feature.id}")
        evidence["features"][feature.id] = record
    evidence["failures"] = failures
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if failures:
        print("Carry verification: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Carry verification: PASS ({len(selected)} impacted features)")
    return 0


def report(args: argparse.Namespace) -> int:
    registry = load(args)
    result = coverage(registry, args.against)
    print(json.dumps(result, indent=2, sort_keys=True))
    for feature in registry.features:
        state = "provisional" if feature.provisional_until else "connected"
        print(f"{feature.id}\t{feature.classification}\t{state}\t{feature.owner}")
    return 1 if result["uncovered"] else 0


def blame(args: argparse.Namespace) -> int:
    registry = load(args)
    owners = owner_for(registry, args.path)
    exemption = exemption_for(registry, args.path)
    payload = {
        "path": args.path,
        "owners": owners,
        "exemption": exemption.__dict__ if exemption else None,
    }
    print(json.dumps(payload, default=str, sort_keys=True))
    return 0 if owners or exemption else 1


def doctor(args: argparse.Namespace) -> int:
    registry = load(args)
    failures: list[str] = []
    hooks_path = git(registry.root, "config", "--get", "core.hooksPath")
    if hooks_path != ".githooks":
        failures.append(f"core.hooksPath is {hooks_path!r}, expected '.githooks'")
    for hook in ("pre-commit", "pre-push", "post-commit", "post-checkout", "post-merge"):
        path = registry.root / ".githooks" / hook
        if not path.is_file() or not os.access(path, os.X_OK):
            failures.append(f"missing executable tracked hook: {hook}")
    ledger = Path(git(registry.root, "rev-parse", "--git-dir")) / "hermes-update" / "run.json"
    if ledger.is_file():
        try:
            state = json.loads(ledger.read_text(encoding="utf-8")).get("status", "")
        except (OSError, json.JSONDecodeError):
            failures.append("update ledger is unreadable")
        else:
            if str(state).lower() not in TERMINAL_LEDGER_STATES:
                failures.append(f"update ledger is non-terminal: {state}")
    if failures:
        print("Carry doctor: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(registry.manifest_path.read_bytes()).hexdigest()
    print(f"Carry doctor: PASS manifest_sha256={digest}")
    return 0


def gate_fingerprint(root: Path, scope: str) -> str:
    if scope == "pre-commit":
        payload = command(root, "git", "diff", "--cached", "--binary").stdout
    else:
        payload = git(root, "rev-parse", "HEAD")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def override_path(root: Path) -> Path:
    raw = Path(git(root, "rev-parse", "--git-dir"))
    git_directory = raw if raw.is_absolute() else root / raw
    directory = git_directory / "hermes-gates"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def create_override(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    directory = override_path(root)
    payload = {
        "scope": args.scope,
        "reason": args.reason,
        "fingerprint": gate_fingerprint(root, args.scope),
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + 300,
    }
    token = directory / "override-next.json"
    token.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(token, 0o600)
    audit = directory / "override.log"
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    os.chmod(audit, 0o600)
    print(f"Carry override armed for one {args.scope} attempt: {args.reason}")
    return 0


def consume_override(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    token = override_path(root) / "override-next.json"
    if not token.is_file():
        return 1
    try:
        payload = json.loads(token.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        token.unlink(missing_ok=True)
        return 1
    valid = (
        payload.get("scope") == args.scope
        and int(payload.get("expires_at", 0)) >= int(time.time())
        and payload.get("fingerprint") == gate_fingerprint(root, args.scope)
    )
    token.unlink(missing_ok=True)
    if valid:
        print(f"Carry gate override consumed: {payload.get('reason', 'unspecified')}")
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    validating = sub.add_parser("validate")
    validating.add_argument("--against")
    validating.set_defaults(func=validate)
    verifying = sub.add_parser("verify")
    verifying.add_argument("--feature", action="append")
    verifying.add_argument("--changed-since")
    verifying.add_argument("--skip-runtime-probes", action="store_true")
    verifying.add_argument("--probes-only", action="store_true")
    verifying.add_argument("--json", type=Path)
    verifying.set_defaults(func=verify)
    reporting = sub.add_parser("report")
    reporting.add_argument("--against")
    reporting.set_defaults(func=report)
    blaming = sub.add_parser("blame")
    blaming.add_argument("path")
    blaming.set_defaults(func=blame)
    diagnosing = sub.add_parser("doctor")
    diagnosing.set_defaults(func=doctor)
    overriding = sub.add_parser("override")
    overriding.add_argument("--scope", choices=("pre-commit", "pre-push"), required=True)
    overriding.add_argument("--reason", required=True)
    overriding.set_defaults(func=create_override)
    consuming = sub.add_parser("consume-override")
    consuming.add_argument("--scope", choices=("pre-commit", "pre-push"), required=True)
    consuming.set_defaults(func=consume_override)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (OSError, ValueError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        print(f"Carry registry: FAIL\n- {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
