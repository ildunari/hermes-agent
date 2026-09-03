"""Load separately versioned local support modules behind narrow core seams."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
from pathlib import Path
from types import ModuleType


_MODULES: dict[str, ModuleType] = {}
_LOCK = threading.RLock()


def _source_candidates(relative_source: Path) -> tuple[Path, ...]:
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    homes = []
    if configured_home:
        homes.append(Path(configured_home).expanduser())
    homes.append(Path.home() / ".hermes")

    candidates: list[Path] = []
    for home in homes:
        candidate = home / "plugins" / relative_source
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _load_source(import_name: str, path: Path) -> ModuleType:
    synthetic_name = "_hermes_external_" + import_name.replace(".", "_")
    spec = importlib.util.spec_from_file_location(synthetic_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load support module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[synthetic_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(synthetic_name, None)
        raise
    return module


def load_support_module(import_name: str, relative_source: str) -> ModuleType:
    """Import a support module from its package or active plugins tree."""
    with _LOCK:
        cached = _MODULES.get(import_name)
        if cached is not None:
            return cached

        failures: list[str] = []
        for candidate in _source_candidates(Path(relative_source)):
            if not candidate.is_file():
                continue
            try:
                module = _load_source(import_name, candidate)
            except Exception as exc:
                failures.append(f"{candidate}: {exc}")
                continue
            _MODULES[import_name] = module
            return module

        try:
            module = importlib.import_module(import_name)
        except ModuleNotFoundError as exc:
            root_package = import_name.partition(".")[0]
            if exc.name not in {import_name, root_package}:
                raise
            failures.append(f"installed package: {exc}")
        else:
            _MODULES[import_name] = module
            return module

        detail = f" ({'; '.join(failures)})" if failures else ""
        raise RuntimeError(
            f"Hermes support module {import_name!r} is unavailable under the "
            f"active profile's plugins/support tree or as an installed package{detail}."
        )
