from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "check_local_carry_contract.py"
)


def _run_contract(root: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--manifest",
            str(manifest),
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def test_forbidden_needles_fail_the_carry_contract(tmp_path: Path) -> None:
    source = tmp_path / "sidebar.tsx"
    source.write_text("const className = 'max-h-44';\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """\
version: 1
checks:
  - id: uncapped-pins
    path: sidebar.tsx
    needles: [className]
    forbidden_needles: [max-h-44]
    description: pinned sessions remain uncapped
features: []
""",
        encoding="utf-8",
    )

    result = _run_contract(tmp_path, manifest)

    assert result.returncode == 1
    assert "FORBIDDEN SYMBOL: sidebar.tsx: 'max-h-44' [uncapped-pins]" in result.stderr


def test_forbidden_needles_allow_a_clean_carry_surface(tmp_path: Path) -> None:
    source = tmp_path / "sidebar.tsx"
    source.write_text("const className = 'outer-scroll';\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """\
version: 1
checks:
  - id: uncapped-pins
    path: sidebar.tsx
    needles: [className]
    forbidden_needles: [max-h-44]
    description: pinned sessions remain uncapped
features: []
""",
        encoding="utf-8",
    )

    result = _run_contract(tmp_path, manifest)

    assert result.returncode == 0
    assert "Local carry contract: PASS" in result.stdout
