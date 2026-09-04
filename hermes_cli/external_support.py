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

def _root_hermes_home() -> Path:
    """Return the OS-account Hermes root, never a cron profile's fake HOME."""
    explicit = os.environ.get("HERMES_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    apparent_home = Path.home()
    parts = apparent_home.parts
    is_profile_home = any(
        parts[index] == ".hermes" and parts[index + 1] == "profiles"
        for index in range(len(parts) - 1)
    )
    if not is_profile_home:
        return apparent_home / ".hermes"
    try:
        import pwd

        account_home = str(pwd.getpwuid(os.getuid()).pw_dir or "").strip()
        if account_home:
            return Path(account_home) / ".hermes"
    except (ImportError, KeyError, OSError):
        pass
    return apparent_home / ".hermes"


def _source_candidate(relative_source: Path) -> Path:
    """Support is root-global; profile HOME values must not redirect it."""
    return _root_hermes_home() / "plugins" / relative_source

def _load_source(import_name: str, path: Path) -> ModuleType:
    package_name, _, _module_name = import_name.rpartition(".")
    if package_name:
        package_dir = path.parent.resolve()
        loaded_package = sys.modules.get(package_name)
        loaded_paths = {
            Path(item).resolve()
            for item in getattr(loaded_package, "__path__", ())
        }
        if loaded_package is not None and package_dir not in loaded_paths:
            for name in tuple(sys.modules):
                if name == package_name or name.startswith(package_name + "."):
                    sys.modules.pop(name, None)
            loaded_package = None
        if loaded_package is None:
            init_path = package_dir / "__init__.py"
            package_spec = importlib.util.spec_from_file_location(
                package_name,
                init_path,
                submodule_search_locations=[str(package_dir)],
            )
            if package_spec is None or package_spec.loader is None:
                raise ImportError(f"cannot load support package from {init_path}")
            loaded_package = importlib.util.module_from_spec(package_spec)
            sys.modules[package_name] = loaded_package
            package_spec.loader.exec_module(loaded_package)

    spec = importlib.util.spec_from_file_location(import_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load support module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[import_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(import_name, None)
        raise
    return module

def load_support_module(import_name: str, relative_source: str) -> ModuleType:
    """Import a support module from its package or active plugins tree."""
    with _LOCK:
        cached = _MODULES.get(import_name)
        if cached is not None:
            return cached

        failures: list[str] = []
        candidate = _source_candidate(Path(relative_source))
        if candidate.is_file():
            try:
                module = _load_source(import_name, candidate)
            except Exception as exc:
                raise RuntimeError(
                    f"Hermes support module {import_name!r} exists at the "
                    f"authoritative root source path {candidate} but failed to load: {exc}"
                ) from exc
            else:
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
            f"root plugins/support tree or as an installed package{detail}."
        )
