from __future__ import annotations

import os

from agent.transports import codex_app_server as app_server


def test_codex_spawn_env_adds_user_cli_paths(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = app_server._codex_spawn_env(codex_home="/tmp/codex-home")

    assert env["CODEX_HOME"] == "/tmp/codex-home"
    parts = env["PATH"].split(":")
    assert os.path.expanduser("~/.npm-global/bin") in parts
    assert "/opt/homebrew/bin" in parts
    assert parts[-2:] == ["/usr/bin", "/bin"]


def test_resolve_codex_bin_uses_augmented_path(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)

    env = {"PATH": str(bin_dir)}

    assert app_server._resolve_codex_bin("codex", env) == str(codex)
    assert app_server._resolve_codex_bin("/custom/codex", env) == "/custom/codex"
