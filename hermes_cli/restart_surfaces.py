"""Compatibility seam for the externally versioned restart implementation."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable


_IMPLEMENTATION: ModuleType | None = None
_SUPPORT_RELATIVE_PATH = Path(
    "support/hermes-studio-ops/src/hermes_studio_ops/restart_surfaces.py"
)


def _support_source_candidates() -> tuple[Path, ...]:
    """Return profile-local then root-local support implementation paths."""
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    homes = []
    if configured_home:
        homes.append(Path(configured_home).expanduser())
    homes.append(Path.home() / ".hermes")

    candidates: list[Path] = []
    for home in homes:
        candidate = home / "plugins" / _SUPPORT_RELATIVE_PATH
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _load_source(path: Path) -> ModuleType:
    module_name = "_hermes_studio_ops_restart_surfaces"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load restart support module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _implementation() -> ModuleType:
    global _IMPLEMENTATION
    if _IMPLEMENTATION is not None:
        return _IMPLEMENTATION

    try:
        _IMPLEMENTATION = importlib.import_module(
            "hermes_studio_ops.restart_surfaces"
        )
        return _IMPLEMENTATION
    except ImportError:
        pass

    failures: list[str] = []
    for candidate in _support_source_candidates():
        if not candidate.is_file():
            continue
        try:
            _IMPLEMENTATION = _load_source(candidate)
            return _IMPLEMENTATION
        except Exception as exc:
            failures.append(f"{candidate}: {exc}")

    detail = f" ({'; '.join(failures)})" if failures else ""
    raise RuntimeError(
        "Hermes restart support is unavailable. Expected the versioned "
        "hermes-studio-ops module under the active profile's plugins/support "
        f"tree or as an installed package{detail}."
    )


def enqueue_detached_restart(*args: Any, **kwargs: Any):
    """Delegate the stable gateway/CLI seam to the support implementation."""
    return _implementation().enqueue_detached_restart(*args, **kwargs)


def main(argv: Iterable[str] | None = None) -> int:
    """Run the external implementation under the historical module command."""
    return int(_implementation().main(argv))


def __getattr__(name: str) -> Any:
    """Preserve imports of implementation symbols during the migration."""
    return getattr(_implementation(), name)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
