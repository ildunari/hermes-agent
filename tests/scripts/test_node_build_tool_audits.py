"""Regression checks for locally audited Node build dependencies."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _locked_package(path: str) -> dict:
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    return lock["packages"][path]


def test_audited_node_build_dependencies_stay_patched() -> None:
    """Pin the patched releases used by the web and TUI build workspaces."""
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock["packages"]
    nested_nanoid = next(
        (
            packages[path]
            for path in (
                "node_modules/postcss/node_modules/nanoid",
                "node_modules/vite/node_modules/nanoid",
            )
            if path in packages
        ),
        None,
    )
    assert nested_nanoid is not None
    assert nested_nanoid["version"] == "3.3.18"
    assert _locked_package("node_modules/node-gyp/node_modules/undici")["version"] == "6.28.0"
    assert _locked_package("node_modules/undici")["version"] == "7.29.0"
