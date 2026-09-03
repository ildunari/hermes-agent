from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

from hermes_cli import external_support, restart_surfaces


def test_enqueue_detached_restart_delegates_to_support_module(monkeypatch):
    calls = []
    implementation = SimpleNamespace(
        enqueue_detached_restart=lambda *args, **kwargs: calls.append((args, kwargs))
        or "queued"
    )
    monkeypatch.setattr(restart_surfaces, "_implementation", lambda: implementation)

    result = restart_surfaces.enqueue_detached_restart(
        "hermes", delay=10, safe_wait_timeout=86400
    )

    assert result == "queued"
    assert calls == [(('hermes',), {"delay": 10, "safe_wait_timeout": 86400})]


def test_historical_module_main_delegates_arguments(monkeypatch):
    implementation = SimpleNamespace(main=lambda argv: 7 if list(argv) == ["--describe"] else 1)
    monkeypatch.setattr(restart_surfaces, "_implementation", lambda: implementation)

    assert restart_surfaces.main(["--describe"]) == 7


def test_detached_bootstrap_failure_writes_private_completion_marker(tmp_path):
    marker = tmp_path / "completion.json"
    missing_home = tmp_path / "missing-home"
    missing_home.mkdir()
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "HERMES_HOME": str(missing_home),
        "PYTHONPATH": str(restart_surfaces.Path(__file__).parents[2]),
    }

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "hermes_cli.restart_surfaces",
            "--scope",
            "hermes",
            "--detached-worker",
            "--completion-marker",
            str(marker),
        ],
        cwd=restart_surfaces.Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["status"] == "complete"
    assert payload["scope"] == "hermes"
    assert payload["exit_code"] == 1
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_external_support_prefers_root_source_package_over_installed(tmp_path, monkeypatch):
    root_home = tmp_path / "root" / ".hermes"
    relative = "support/example/src/example_support/main.py"
    package = root_home / "plugins" / "support/example/src/example_support"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sibling.py").write_text("value = 42\n", encoding="utf-8")
    (package / "main.py").write_text(
        "from example_support.sibling import value\n", encoding="utf-8"
    )
    monkeypatch.setattr(external_support.Path, "home", lambda: tmp_path / "root")
    monkeypatch.setattr(external_support, "_MODULES", {})
    monkeypatch.setattr(
        external_support.importlib,
        "import_module",
        lambda _name: SimpleNamespace(value="stale-installed-shadow"),
    )

    loaded = external_support.load_support_module("example_support.main", relative)

    assert loaded.value == 42
    assert loaded.__file__ == str(package / "main.py")
