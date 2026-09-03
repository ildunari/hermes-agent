from __future__ import annotations

from types import SimpleNamespace

from hermes_cli import restart_surfaces


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


def test_profile_support_tree_is_a_runtime_loading_candidate(tmp_path, monkeypatch):
    source = (
        tmp_path
        / "plugins"
        / "support"
        / "hermes-studio-ops"
        / "src"
        / "hermes_studio_ops"
        / "restart_surfaces.py"
    )
    source.parent.mkdir(parents=True)
    source.write_text("value = 'loaded-from-profile-support'\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(restart_surfaces, "_IMPLEMENTATION", None)

    loaded = restart_surfaces._implementation()

    assert loaded.value == "loaded-from-profile-support"
