"""Focused contracts for release-version propagation."""

import importlib.util
import json
from pathlib import Path


def _load_release_module(root: Path):
    spec = importlib.util.spec_from_file_location(
        "hermes_release_for_test", root / "scripts" / "release.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_version_files_updates_desktop_lock_metadata(tmp_path):
    root = Path(__file__).resolve().parents[2]
    release = _load_release_module(root)
    desktop = tmp_path / "apps" / "desktop"
    desktop.mkdir(parents=True)
    version_file = tmp_path / "hermes_cli" / "__init__.py"
    version_file.parent.mkdir()
    version_file.write_text(
        '__version__ = "0.20.4"\n__release_date__ = "2026.8.18"\n',
        encoding="utf-8",
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.20.4"\n', encoding="utf-8")
    package_json = desktop / "package.json"
    package_json.write_text('{"name":"hermes","version":"0.20.4"}\n', encoding="utf-8")
    package_lock = tmp_path / "package-lock.json"
    package_lock.write_text(
        json.dumps(
            {"packages": {"apps/desktop": {"name": "hermes", "version": "0.20.4"}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    release.REPO_ROOT = tmp_path
    release.VERSION_FILE = version_file
    release.PYPROJECT_FILE = pyproject
    release.PACKAGE_LOCK_FILE = package_lock
    release.update_version_files("0.21.0", "2026.8.20")

    assert json.loads(package_json.read_text())["version"] == "0.21.0"
    lock = json.loads(package_lock.read_text())
    assert lock["packages"]["apps/desktop"]["version"] == "0.21.0"
